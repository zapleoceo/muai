"""media_worker — recognition via broker + failure classification.

Env defaults set before import (media_worker reads them at module load).
"""
# ruff: noqa: I001  # env setup intentionally split around imports
from __future__ import annotations

import os

os.environ.setdefault("INTERNAL_SECRET", "test-internal-secret")
os.environ.setdefault("BROKER_URL", "https://aib.zapleo.com")
os.environ.setdefault("BROKER_PROJECT_KEY", "aib_prj_test")

from unittest.mock import AsyncMock, patch  # noqa: E402

import pytest  # noqa: E402

import media_worker.__main__ as mw  # noqa: E402


# ─── _is_permanent ─────────────────────────────────────────────────────────


def test_permanent_on_client_4xx():
    for e in ("broker vision HTTP 400: bad", "HTTP 401 unauth",
              "http 403 scope", "broker whisper HTTP 413: too big"):
        assert mw._is_permanent(e) is True


def test_transient_on_rate_limit_and_5xx():
    for e in ("broker vision HTTP 429: slow down",
              "broker whisper HTTP 503: no key",
              "broker vision HTTP 502: bad gateway",
              "download: connection reset"):
        assert mw._is_permanent(e) is False


def test_permanent_on_misconfig_and_empty():
    assert mw._is_permanent("BROKER_URL/BROKER_PROJECT_KEY not set") is True
    assert mw._is_permanent("broker vision returned empty text") is True


# ─── _broker_headers ───────────────────────────────────────────────────────


def test_broker_headers_carries_project_key():
    h = mw._broker_headers()
    assert h["X-Project-Key"] == "aib_prj_test"


def test_broker_headers_raises_when_unconfigured(monkeypatch):
    monkeypatch.setattr(mw, "BROKER_URL", "")
    with pytest.raises(RuntimeError, match="BROKER_URL"):
        mw._broker_headers()


# ─── _recognize_photo (broker vision) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_recognize_photo_sends_multimodal_and_returns_text():
    captured = {}

    class FakeResp:
        status_code = 200
        def json(self):
            return {"text": "на фото кот"}

    async def fake_post(self, url, params=None, json=None, headers=None, **kw):
        captured["url"] = url
        captured["params"] = params
        captured["json"] = json
        return FakeResp()

    with patch("httpx.AsyncClient.post", fake_post):
        txt = await mw._recognize_photo("BASE64DATA", "image/jpeg")

    assert txt == "на фото кот"
    assert captured["params"] == {"capability": "vision"}
    content = captured["json"]["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,BASE64")


@pytest.mark.asyncio
async def test_recognize_photo_raises_on_broker_error():
    class FakeResp:
        status_code = 503
        text = "no provider"
        def json(self):
            return {}

    async def fake_post(self, *a, **kw):
        return FakeResp()

    with patch("httpx.AsyncClient.post", fake_post), \
            pytest.raises(RuntimeError, match="503"):
        await mw._recognize_photo("x", "image/png")


@pytest.mark.asyncio
async def test_recognize_photo_raises_on_empty_text():
    class FakeResp:
        status_code = 200
        def json(self):
            return {"text": "   "}

    async def fake_post(self, *a, **kw):
        return FakeResp()

    with patch("httpx.AsyncClient.post", fake_post), \
            pytest.raises(RuntimeError, match="empty text"):
        await mw._recognize_photo("x", "image/png")


# ─── _recognize_audio (broker whisper) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_recognize_audio_returns_text():
    class FakeResp:
        status_code = 200
        def json(self):
            return {"text": "привет это голосовое"}

    async def fake_post(self, url, params=None, files=None, headers=None, **kw):
        assert "transcribe" in url
        assert files is not None
        return FakeResp()

    with patch("httpx.AsyncClient.post", fake_post):
        txt = await mw._recognize_audio(b"oggbytes", "audio/ogg")
    assert txt == "привет это голосовое"


@pytest.mark.asyncio
async def test_recognize_audio_rejects_oversize():
    big = b"x" * (mw._MAX_AUDIO_BYTES + 1)
    with pytest.raises(RuntimeError, match="413"):
        await mw._recognize_audio(big, "audio/ogg")


# ─── _process_one routing ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_one_missing_metadata():
    seg, err = await mw._process_one({"id": 1, "content_text": "", "metadata": {}})
    assert seg == ""
    assert "missing" in err


@pytest.mark.asyncio
async def test_process_one_photo_happy():
    row = {"id": 1, "content_text": "[photo]",
           "metadata": {"chat_id": 1, "msg_id": 2, "media_kind": "photo"}}
    with patch.object(mw, "_download",
                      AsyncMock(return_value=(b"img", "image/jpeg", None))), \
         patch.object(mw, "_recognize_photo",
                      AsyncMock(return_value="кот на диване")):
        seg, err = await mw._process_one(row)
    assert err is None
    assert "кот на диване" in seg


@pytest.mark.asyncio
async def test_process_one_download_fail_returns_err():
    row = {"id": 1, "content_text": "[photo]",
           "metadata": {"chat_id": 1, "msg_id": 2, "media_kind": "photo"}}
    with patch.object(mw, "_download",
                      AsyncMock(return_value=(None, None, "deleted"))):
        seg, err = await mw._process_one(row)
    assert seg == ""
    assert "download" in err


@pytest.mark.asyncio
async def test_process_one_sticker_goes_through_vision():
    """Stickers (static webp) are recognized via vision, labelled distinctly."""
    row = {"id": 1, "content_text": "[sticker: 😂]",
           "metadata": {"chat_id": 1, "msg_id": 2, "media_kind": "sticker"}}
    with patch.object(mw, "_download",
                      AsyncMock(return_value=(b"webp", "image/webp", None))), \
         patch.object(mw, "_recognize_photo",
                      AsyncMock(return_value="смеющийся персонаж")):
        seg, err = await mw._process_one(row)
    assert err is None
    assert "смеющийся персонаж" in seg
    assert "recognized sticker" in seg


@pytest.mark.asyncio
async def test_process_one_voice_happy():
    row = {"id": 1, "content_text": "[voice: 5s]",
           "metadata": {"chat_id": 1, "msg_id": 2, "media_kind": "voice"}}
    with patch.object(mw, "_download",
                      AsyncMock(return_value=(b"ogg", "audio/ogg", None))), \
         patch.object(mw, "_recognize_audio",
                      AsyncMock(return_value="привет")):
        seg, err = await mw._process_one(row)
    assert err is None
    assert "привет" in seg
    assert "voice transcription" in seg
