"""ingestor_telegram.send_guard + POST /actions/send_message.

The one place the userbot posts AS DIMA (monthly banya report). Every guard
is pinned here: wrong/empty secret, chat outside the allowlist, empty/huge
text, and that the endpoint never shows up in the agent's /tools/spec."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(__file__), "..", "..",
    "services", "ingestor-telegram", "src"))

from fastapi.testclient import TestClient  # noqa: E402
from ingestor_telegram.send_guard import (  # noqa: E402
    MAX_TEXT,
    allowed_chats,
    check_send_request,
    send_to_chat,
)
from ingestor_telegram.tools_http import build_app  # noqa: E402

CHAT = "-1003799072880"
OK = {"send_secret": "s3cret", "allowed": {CHAT}}


# ─── check_send_request ──────────────────────────────────────────────────────

def test_allows_valid_request():
    assert check_send_request("s3cret", CHAT, "hi", **OK) is None


@pytest.mark.parametrize("header", [None, "", "wrong"])
def test_rejects_bad_secret(header):
    assert check_send_request(header, CHAT, "hi", **OK)[0] == 401


def test_unconfigured_secret_refuses_everyone():
    # пустой TG_SEND_SECRET не должен превращаться в «открыто всем»
    assert check_send_request("", CHAT, "hi", send_secret="", allowed={CHAT})[0] == 401


@pytest.mark.parametrize("chat", [None, "", "-100999", "3799072880"])
def test_rejects_chat_outside_allowlist(chat):
    assert check_send_request("s3cret", chat, "hi", **OK)[0] == 403


def test_empty_allowlist_refuses():
    assert check_send_request("s3cret", CHAT, "hi", send_secret="s3cret", allowed=set())[0] == 403


@pytest.mark.parametrize("text", [None, "", "   ", 123, "x" * (MAX_TEXT + 1)])
def test_rejects_bad_text(text):
    assert check_send_request("s3cret", CHAT, text, **OK)[0] == 400


def test_allowed_chats_parses_env(monkeypatch):
    monkeypatch.setenv("TG_SEND_ALLOWED_CHATS", f" {CHAT} , ,-100123 ")
    assert allowed_chats() == {CHAT, "-100123"}
    monkeypatch.delenv("TG_SEND_ALLOWED_CHATS")
    assert allowed_chats() == set()


# ─── send_to_chat ────────────────────────────────────────────────────────────

def _dialogs(*items):
    async def gen():
        for it in items:
            yield it
    return MagicMock(side_effect=lambda: gen())


@pytest.mark.asyncio
async def test_send_uses_cached_entity():
    client = MagicMock()
    client.get_entity = AsyncMock(return_value="ENT")
    client.send_message = AsyncMock(return_value=SimpleNamespace(id=745))
    assert await send_to_chat(client, int(CHAT), "hi") == 745
    client.send_message.assert_awaited_once_with("ENT", "hi")


@pytest.mark.asyncio
async def test_send_falls_back_to_dialogs_when_cache_cold():
    client = MagicMock()
    client.get_entity = AsyncMock(side_effect=ValueError("Could not find the input entity"))
    client.iter_dialogs = _dialogs(
        SimpleNamespace(id=-100111, entity="OTHER"),
        SimpleNamespace(id=int(CHAT), entity="BANYA"),
    )
    client.send_message = AsyncMock(return_value=SimpleNamespace(id=7))
    assert await send_to_chat(client, int(CHAT), "hi") == 7
    client.send_message.assert_awaited_once_with("BANYA", "hi")


@pytest.mark.asyncio
async def test_send_raises_when_chat_unknown():
    client = MagicMock()
    client.get_entity = AsyncMock(side_effect=ValueError("nope"))
    client.iter_dialogs = _dialogs(SimpleNamespace(id=-100111, entity="OTHER"))
    client.send_message = AsyncMock()
    with pytest.raises(LookupError):
        await send_to_chat(client, int(CHAT), "hi")
    client.send_message.assert_not_awaited()


# ─── POST /actions/send_message ──────────────────────────────────────────────

@pytest.fixture
def app_client(monkeypatch):
    monkeypatch.setenv("TG_SEND_SECRET", "s3cret")
    monkeypatch.setenv("TG_SEND_ALLOWED_CHATS", CHAT)
    tg = MagicMock()
    tg.get_entity = AsyncMock(return_value="ENT")
    tg.send_message = AsyncMock(return_value=SimpleNamespace(id=900))
    return TestClient(build_app(tg)), tg


def test_endpoint_posts_and_returns_message_id(app_client):
    http, tg = app_client
    r = http.post("/actions/send_message", json={"chat_id": CHAT, "text": "Сумма"},
                  headers={"X-Send-Secret": "s3cret"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "chat_id": int(CHAT), "message_id": 900}
    tg.send_message.assert_awaited_once_with("ENT", "Сумма")


def test_endpoint_refuses_internal_secret(app_client):
    # INTERNAL_SECRET есть у всех сервисов (и у агента) — им сюда нельзя
    http, tg = app_client
    r = http.post("/actions/send_message", json={"chat_id": CHAT, "text": "x"},
                  headers={"X-Internal-Secret": "s3cret"})
    assert r.status_code == 401
    tg.send_message.assert_not_awaited()


def test_endpoint_refuses_foreign_chat(app_client):
    http, tg = app_client
    r = http.post("/actions/send_message", json={"chat_id": "-100555", "text": "x"},
                  headers={"X-Send-Secret": "s3cret"})
    assert r.status_code == 403
    tg.send_message.assert_not_awaited()


def test_endpoint_maps_send_failure_to_502(app_client):
    http, tg = app_client
    tg.send_message = AsyncMock(side_effect=RuntimeError("flood"))
    r = http.post("/actions/send_message", json={"chat_id": CHAT, "text": "x"},
                  headers={"X-Send-Secret": "s3cret"})
    assert r.status_code == 502


def test_not_exposed_to_the_agent(app_client):
    http, _ = app_client
    names = [s["name"] for s in http.get("/tools/spec").json()]
    assert not any("send" in n for n in names)
    # и по пути агента (/tools/{short}) эндпоинта не существует
    assert http.post("/tools/send_message", json={}).status_code == 404
