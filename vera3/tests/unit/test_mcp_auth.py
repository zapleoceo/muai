"""Удалённый MCP: bearer-аутентификация и сквозной вызов инструмента по HTTP."""
from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from vera_mcp.auth import (
    BearerAuthMiddleware,
    WeakTokenError,
    bearer_of,
    client_of,
    load_tokens,
    match_token,
    validate_tokens,
)
from vera_mcp.server import build_app

CLAUDE_TOKEN = "claude-token-0123456789-0123456789"
CODEX_TOKEN = "codex-token-abcdefghij-abcdefghij"
MCP_HEADERS = {"Accept": "application/json, text/event-stream",
               "Content-Type": "application/json"}


def _rpc(method: str, params: dict | None = None) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}


# ─── разбор токенов ───────────────────────────────────────────────────────


def test_load_tokens_single_and_named():
    got = load_tokens({"MCP_TOKEN": CLAUDE_TOKEN,
                       "MCP_TOKENS": f"codex:{CODEX_TOKEN}, desktop:{CLAUDE_TOKEN}x"})
    assert got == {CLAUDE_TOKEN: "default", CODEX_TOKEN: "codex",
                   CLAUDE_TOKEN + "x": "desktop"}


def test_load_tokens_fail_closed_when_unset():
    assert load_tokens({}) == {}
    assert load_tokens({"MCP_TOKEN": "  ", "MCP_TOKENS": " , "}) == {}


def test_load_tokens_drops_short_and_names_unnamed():
    got = load_tokens({"MCP_TOKENS": f"short:abc,{CODEX_TOKEN}"})
    assert got == {CODEX_TOKEN: "token2"}


def test_match_token_returns_client_name():
    tokens = {CLAUDE_TOKEN: "claude", CODEX_TOKEN: "codex"}
    assert match_token(CODEX_TOKEN, tokens) == "codex"
    assert match_token("nope", tokens) is None
    assert match_token(None, tokens) is None
    assert match_token("", tokens) is None
    assert match_token(CODEX_TOKEN, {}) is None


def test_bearer_of_parses_header():
    assert bearer_of([(b"authorization", b"Bearer abc")]) == "abc"
    assert bearer_of([(b"Authorization", b"bearer abc")]) == "abc"
    assert bearer_of([(b"authorization", b"Basic abc")]) is None
    assert bearer_of([(b"x-other", b"Bearer abc")]) is None


def test_client_of_reads_scope():
    ctx = SimpleNamespace(request_context=SimpleNamespace(
        request=SimpleNamespace(scope={"mcp_client": "codex"})))
    assert client_of(ctx) == "codex"
    ctx.request_context.request.scope = {}
    assert client_of(ctx) == "unknown"


# ─── HTTP: сервер целиком ─────────────────────────────────────────────────


@asynccontextmanager
async def _open_client():
    app = build_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            yield c


@pytest.fixture
def mcp_client(monkeypatch, sqlite_db):
    monkeypatch.setenv("MCP_TOKENS", f"claude:{CLAUDE_TOKEN},codex:{CODEX_TOKEN}")
    return _open_client


def _auth(token: str) -> dict:
    return {**MCP_HEADERS, "Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_no_token_is_401(mcp_client):
    async with mcp_client() as c:
        r = await c.post("/mcp", json=_rpc("tools/list"), headers=MCP_HEADERS)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


@pytest.mark.asyncio
async def test_wrong_token_is_401(mcp_client):
    async with mcp_client() as c:
        r = await c.post("/mcp", json=_rpc("tools/list"), headers=_auth("wrong"))
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_unset_tokens_fail_closed(monkeypatch, mcp_client):
    monkeypatch.delenv("MCP_TOKENS")
    async with mcp_client() as c:
        r = await c.post("/mcp", json=_rpc("tools/list"), headers=_auth(CLAUDE_TOKEN))
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_healthz_needs_no_token(mcp_client):
    async with mcp_client() as c:
        r = await c.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_right_token_lists_every_tool(mcp_client):
    async with mcp_client() as c:
        r = await c.post("/mcp", json=_rpc("tools/list"), headers=_auth(CLAUDE_TOKEN))
    assert r.status_code == 200
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert names == {
        "search", "recent_events", "get_event", "list_sources", "entity_find",
        "entity_context", "graph_neighbours", "timeline", "sql_query", "audit_log",
        "remember", "update_event", "hide_event", "unhide_event", "entity_rename",
        "entity_add_alias", "relationship_set", "relationship_retire", "entity_merge",
        "entity_unmerge", "undo", "event_participants", "co_occurrence", "voice_speaker_set",
        "entity_add_nickname"}


@pytest.mark.asyncio
async def test_write_through_http_is_audited_under_the_token_name(mcp_client):
    from datetime import datetime

    from vera_shared.db.engine import get_session
    from vera_shared.db.models import EventRow

    async with get_session() as s:
        s.add(EventRow(id=7, source="gmail", source_event_id="a", content_text="hello",
                       occurred_at=datetime(2026, 1, 1), triage_status="done"))
    async with mcp_client() as c:
        r = await c.post("/mcp", json=_rpc(
            "tools/call", {"name": "hide_event", "arguments": {"event_id": 7}}),
            headers=_auth(CODEX_TOKEN))
        assert r.status_code == 200
        assert not r.json()["result"].get("isError")
        r = await c.post("/mcp", json=_rpc(
            "tools/call", {"name": "audit_log", "arguments": {}}),
            headers=_auth(CODEX_TOKEN))
    entries = r.json()["result"]["structuredContent"]["entries"]
    assert [(e["client"], e["tool"], e["target"]) for e in entries] == [
        ("codex", "hide_event", "event:7")]


@pytest.mark.asyncio
async def test_middleware_passes_lifespan_through():
    seen = []

    async def app(scope, receive, send):
        seen.append(scope["type"])

    await BearerAuthMiddleware(app)({"type": "lifespan"}, None, None)
    assert seen == ["lifespan"]


def test_validate_tokens_fails_on_a_short_token_naming_the_client():
    with pytest.raises(WeakTokenError, match="codex"):
        validate_tokens({"MCP_TOKENS": f"claude:{CLAUDE_TOKEN},codex:short-token"})
    assert validate_tokens({"MCP_TOKENS": f"claude:{CLAUDE_TOKEN}"}) == {CLAUDE_TOKEN: "claude"}
    assert validate_tokens({}) == {}


def test_main_refuses_to_start_with_a_weak_token(monkeypatch):
    from vera_mcp import __main__ as entry

    monkeypatch.setenv("MCP_TOKEN", "tooshort")
    with pytest.raises(WeakTokenError):
        entry.main()


@pytest.mark.asyncio
async def test_websocket_is_rejected_not_passed_through():
    reached = []
    sent = []

    async def app(scope, receive, send):
        reached.append(scope["type"])

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        sent.append(message)

    await BearerAuthMiddleware(app)({"type": "websocket", "path": "/mcp", "headers": []},
                                    receive, send)
    assert reached == []
    assert sent[0]["type"] == "websocket.close" and sent[0]["code"] == 1008
