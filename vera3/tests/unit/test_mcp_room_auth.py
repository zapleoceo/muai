"""Токен комнаты видит только room_*-инструменты, токен владельца — только память."""
from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
import pytest
from vera_mcp.auth import (
    WeakTokenError,
    allowed_rooms,
    load_room_tokens,
    validate_room_tokens,
)
from vera_mcp.server import build_app

OWNER_TOKEN = "owner-token-0123456789-0123456789"
ROOM_CLAUDE = "room-claude-0123456789-0123456789"
ROOM_CODEX = "room-codex-abcdefghij-abcdefghij"
HEADERS = {"Accept": "application/json, text/event-stream",
           "Content-Type": "application/json"}


def _rpc(method: str, params: dict | None = None) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}


def _auth(token: str) -> dict:
    return {**HEADERS, "Authorization": f"Bearer {token}"}


@asynccontextmanager
async def _open_client():
    app = build_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            yield c


@pytest.fixture
def room_client(monkeypatch, sqlite_db):
    monkeypatch.setenv("MCP_TOKENS", f"claude:{OWNER_TOKEN}")
    monkeypatch.setenv("ROOM_TOKENS", f"claude:{ROOM_CLAUDE},codex:{ROOM_CODEX}")
    return _open_client


async def _tool_names(c: httpx.AsyncClient, token: str) -> set[str]:
    r = await c.post("/mcp", json=_rpc("tools/list"), headers=_auth(token))
    assert r.status_code == 200
    return {t["name"] for t in r.json()["result"]["tools"]}


def test_room_tokens_parse_and_drop_short():
    assert load_room_tokens({"ROOM_TOKENS": f"codex:{ROOM_CODEX},x:short"}) == {
        ROOM_CODEX: "codex"}


def test_room_token_must_not_reuse_an_owner_token():
    with pytest.raises(ValueError, match="reuse an MCP token"):
        validate_room_tokens({"MCP_TOKENS": f"claude:{OWNER_TOKEN}",
                              "ROOM_TOKENS": f"claude:{OWNER_TOKEN}"})


def test_same_room_token_under_two_names_refuses_start():
    with pytest.raises(ValueError, match="same token twice"):
        validate_room_tokens({"ROOM_TOKENS": f"claude:{ROOM_CODEX},codex:{ROOM_CODEX}"})


def test_allowed_rooms_default_to_main():
    assert allowed_rooms({}) == {"main"}
    assert allowed_rooms({"ROOM_NAMES": "main, proj-a ,"}) == {"main", "proj-a"}


def test_short_room_token_refuses_start():
    with pytest.raises(WeakTokenError, match="ROOM tokens for codex"):
        validate_room_tokens({"ROOM_TOKENS": "codex:short"})


@pytest.mark.asyncio
async def test_room_token_sees_only_room_tools(room_client):
    async with room_client() as c:
        names = await _tool_names(c, ROOM_CODEX)
    assert names == {"room_post", "room_inbox", "room_ack", "room_history", "room_task_open",
                     "room_task_claim", "room_task_update", "room_task_progress",
                     "room_task_state", "room_task_history", "room_task_wait",
                     "room_task_next", "room_task_release", "room_tasks", "room_task_ask",
                     "room_task_answer_ack", "room_task_questions", "room_task_handoff",
                     "room_task_handoff_accept", "room_task_handoff_decline"}


@pytest.mark.asyncio
async def test_owner_token_does_not_get_room_tools(room_client):
    async with room_client() as c:
        names = await _tool_names(c, OWNER_TOKEN)
    assert "sql_query" in names and not any(n.startswith("room_") for n in names)


@pytest.mark.asyncio
async def test_room_token_cannot_call_memory_tools(room_client):
    async with room_client() as c:
        r = await c.post("/mcp", json=_rpc(
            "tools/call", {"name": "sql_query", "arguments": {"query": "select 1"}}),
            headers=_auth(ROOM_CODEX))
    assert r.json()["result"]["isError"] is True


@pytest.mark.asyncio
async def test_message_round_trip_between_agents_over_http(room_client):
    async with room_client() as c:
        r = await c.post("/mcp", json=_rpc("tools/call", {
            "name": "room_post", "arguments": {"body": "привет", "to": "codex",
                                               "status": "request"}}),
            headers=_auth(ROOM_CLAUDE))
        assert not r.json()["result"].get("isError")
        r = await c.post("/mcp", json=_rpc(
            "tools/call", {"name": "room_inbox", "arguments": {}}),
            headers=_auth(ROOM_CODEX))
    (msg,) = r.json()["result"]["structuredContent"]["messages"]
    assert (msg["from"], msg["to"], msg["body"]) == ("claude", "codex", "привет")


@pytest.mark.asyncio
async def test_healthz_still_open(room_client):
    async with room_client() as c:
        assert (await c.get("/healthz")).status_code == 200
