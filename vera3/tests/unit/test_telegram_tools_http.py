"""ingestor-telegram tools-server: маршруты тонкие, логика в chat_tools /
media_payload, секрет сравнивается общей постоянной по времени функцией."""
from __future__ import annotations

import inspect
from base64 import b64decode
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from ingestor_telegram import tools_http
from ingestor_telegram.media_download import MediaTooLarge
from ingestor_telegram.media_payload import media_mime, media_payload

SECRET = {"X-Internal-Secret": "test-internal-secret"}


def _client(tg) -> TestClient:
    return TestClient(tools_http.build_app(tg))


def test_secret_check_uses_shared_constant_time_helper():
    src = inspect.getsource(tools_http._check_secret)
    assert "internal_secret_ok" in src and "!=" not in src


@pytest.mark.parametrize("headers", [{}, {"X-Internal-Secret": "wrong"}])
def test_tools_refuse_bad_secret(headers):
    tg = MagicMock()
    r = _client(tg).post("/tools/find_user", json={"q": "x"}, headers=headers)
    assert r.status_code == 401 and r.json()["detail"] == "X-Internal-Secret required"


def test_empty_configured_secret_refuses_everyone(monkeypatch):
    monkeypatch.setattr(tools_http, "INTERNAL_SECRET", "")
    r = _client(MagicMock()).post("/tools/find_user", json={"q": "x"},
                                  headers={"X-Internal-Secret": ""})
    assert r.status_code == 401


def test_find_user_error_is_a_payload_not_500():
    tg = MagicMock()
    tg.get_entity = AsyncMock(side_effect=ValueError("no such user"))
    r = _client(tg).post("/tools/find_user", json={"q": "ghost"}, headers=SECRET)
    assert r.status_code == 200 and r.json() == {"error": "ValueError: no such user"}
    tg.get_entity.assert_awaited_once_with("@ghost")


def test_chat_not_found_is_reported():
    tg = MagicMock()

    async def no_dialogs():
        return
        yield

    tg.iter_dialogs = MagicMock(return_value=no_dialogs())
    r = _client(tg).post("/tools/get_chat_info", json={"chat_query": "nowhere"},
                         headers=SECRET)
    assert r.json() == {"error": "chat not found", "query": "nowhere"}


def test_media_route_passes_ints_to_the_service():
    payload = AsyncMock(return_value={"error": "message not found"})
    with patch.object(tools_http, "media_payload", payload):
        r = _client(MagicMock()).post("/media/download",
                                      json={"chat_id": "-100", "msg_id": "5"},
                                      headers=SECRET)
    assert r.json() == {"error": "message not found"}
    assert payload.await_args.args[1:] == (-100, 5)


def test_mime_by_media_kind():
    assert media_mime(SimpleNamespace(voice=1)) == "audio/ogg"
    assert media_mime(SimpleNamespace(voice=None, audio=None, photo=1)) == "image/jpeg"
    doc = SimpleNamespace(voice=None, audio=None, photo=None, sticker=None,
                          document=SimpleNamespace(mime_type="application/pdf"))
    assert media_mime(doc) == "application/pdf"
    assert media_mime(SimpleNamespace()) is None


@pytest.mark.asyncio
async def test_media_payload_encodes_bytes():
    msg = SimpleNamespace(media=1, voice=1)
    tg = MagicMock()
    tg.get_messages = AsyncMock(return_value=msg)
    with patch("ingestor_telegram.media_payload.download_capped",
               AsyncMock(return_value=b"ogg")):
        out = await media_payload(tg, 1, 2)
    assert b64decode(out["b64"]) == b"ogg" and out["mime"] == "audio/ogg" and out["size"] == 3


@pytest.mark.asyncio
async def test_media_payload_reports_cap_and_missing():
    tg = MagicMock()
    tg.get_messages = AsyncMock(return_value=SimpleNamespace(media=1))
    with patch("ingestor_telegram.media_payload.download_capped",
               AsyncMock(side_effect=MediaTooLarge(99))):
        assert (await media_payload(tg, 1, 2))["error"] == str(MediaTooLarge(99))
    tg.get_messages = AsyncMock(return_value=SimpleNamespace(media=None))
    assert await media_payload(tg, 1, 2) == {"error": "no media on this message"}
    tg.get_messages = AsyncMock(side_effect=RuntimeError("flood"))
    assert await media_payload(tg, 1, 2) == {"error": "RuntimeError: flood"}
