"""MCP Events: server/discover и events/list по реальному JSON на проводе (ASGI-клиент)."""
from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
import pytest
from vera_mcp.server import build_app

OWNER = "owner-token-0123456789-0123456789"
ROOM = "room-claude-0123456789-0123456789"
MODERN = "2026-07-28"
ENVELOPE = {"io.modelcontextprotocol/protocolVersion": MODERN,
            "io.modelcontextprotocol/clientCapabilities": {}}


def headers(token: str, method: str) -> dict[str, str]:
    return {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
            "Authorization": f"Bearer {token}", "MCP-Protocol-Version": MODERN, "Mcp-Method": method}


@asynccontextmanager
async def open_client():
    app = build_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            yield c


async def rpc(c: httpx.AsyncClient, token: str, method: str, params: dict | None = None) -> dict:
    body = {"jsonrpc": "2.0", "id": 1, "method": method,
            "params": {**(params or {}), "_meta": ENVELOPE}}
    r = await c.post("/mcp", json=body, headers=headers(token, method))
    return r.json()


@pytest.fixture(autouse=True)
def _tokens(monkeypatch, sqlite_db):
    monkeypatch.setenv("MCP_TOKENS", f"claude:{OWNER}")
    monkeypatch.setenv("ROOM_TOKENS", f"claude:{ROOM}")


@pytest.mark.asyncio
async def test_c1_discover_advertises_tools_and_events():
    async with open_client() as c:
        result = (await rpc(c, ROOM, "server/discover"))["result"]
    assert MODERN in result["supportedVersions"]
    assert "tools" in result["capabilities"]
    assert result["capabilities"]["events"] == {}


@pytest.mark.asyncio
async def test_c1_owner_realm_does_not_advertise_events():
    async with open_client() as c:
        result = (await rpc(c, OWNER, "server/discover"))["result"]
    assert "events" not in result["capabilities"]


@pytest.mark.asyncio
async def test_c2_events_list_shape():
    async with open_client() as c:
        result = (await rpc(c, ROOM, "events/list"))["result"]
    events = {e["name"]: e for e in result["events"]}
    assert set(events) == {"ready", "result", "blocker"}
    for event in events.values():
        assert event["delivery"] == ["webhook"]
        props = event["inputSchema"]["properties"]
        assert set(props) == {"room", "task_id", "kind"}
        assert props["kind"]["type"] == "array"
        assert event["inputSchema"]["required"] == ["room"]
        assert {"task_id", "revision", "kind", "correlation_id", "causation_id", "actor"} <= set(
            event["payloadSchema"]["properties"])


@pytest.mark.asyncio
async def test_c2_events_list_requires_auth():
    async with open_client() as c:
        r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "events/list"},
                         headers=headers("bad-token", "events/list"))
    assert r.status_code == 401
