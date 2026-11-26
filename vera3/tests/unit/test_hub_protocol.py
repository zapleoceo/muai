"""MCP-хаб: протокол, права агентов и изоляция секретов через HTTP-вход /hub/mcp."""
from __future__ import annotations

from typing import Any

import httpx
import pytest
from hub_support import HUB_KEY, SECRET, FakeUpstream, router, row
from vera_hub.app import MCP_PATH, build_app
from vera_hub.auth import AgentAuth
from vera_hub.calls import drain_calls
from vera_hub.upstreams import AGENT_HEADER, Registry

TOKEN = "vhub_" + "t" * 40
pytestmark = pytest.mark.asyncio


class _Agent:
    def __init__(self, allow: list[str]) -> None:
        self.id, self.name, self.allow = 7, "probe", allow


@pytest.fixture
def calls(monkeypatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    async def fake_insert(fields: dict[str, Any]) -> None:
        seen.append(fields)
    monkeypatch.setattr("vera_hub.calls.insert_call", fake_insert)
    return seen


@pytest.fixture
def hub(monkeypatch, calls):
    monkeypatch.setenv("HUB_SECRET_KEY", HUB_KEY)
    from vera_shared.hub.secrets import seal
    fakes = {"cat.example": FakeUpstream(["search", "guide"]),
             "vid.example": FakeUpstream(["generate"], sse=True),
             "mcp": FakeUpstream(["room_inbox"], session=False)}
    rows = [
        row(1, "sniffer", "catalog", "https://cat.example/mcp"),
        row(2, "aib", "video", "https://vid.example/mcp/video", auth_kind="bearer",
            secret_enc=seal(SECRET)),
        row(3, "vera", "room", "http://mcp:8000/mcp", auth_kind="bearer", secret_enc=seal(SECRET)),
        row(4, "itstep", "crm", "https://crm.example/mcp", auth_kind="url"),
    ]

    async def loader():
        return rows

    async def lookup(token: str):
        return _Agent(["sniffer", "vera/room", "itstep"]) if token == TOKEN else None

    async def touch(_id: int) -> None:
        return None

    reg = Registry(loader=loader, http=router(fakes))
    app = build_app(reg, AgentAuth(lookup, touch), background=False)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://hub")
    return client, fakes


async def _rpc(client: httpx.AsyncClient, method: str, params: dict | None = None,
               token: str = TOKEN, rid: int = 1) -> httpx.Response:
    body = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
    resp = await client.post(MCP_PATH, json=body, headers={"Authorization": f"Bearer {token}"})
    await drain_calls()
    return resp


async def test_initialize_and_ping(hub):
    client, _ = hub
    r = await _rpc(client, "initialize", {"protocolVersion": "2025-03-26"})
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["protocolVersion"] == "2025-03-26"
    assert result["capabilities"]["tools"] == {"listChanged": False}
    assert (await _rpc(client, "ping")).json()["result"] == {}


async def test_unknown_protocol_version_falls_back_to_latest(hub):
    client, _ = hub
    r = await _rpc(client, "initialize", {"protocolVersion": "1999-01-01"})
    assert r.json()["result"]["protocolVersion"] == "2025-06-18"


async def test_tools_list_shows_only_granted_upstreams_with_prefixes(hub, calls):
    client, _ = hub
    names = {t["name"] for t in (await _rpc(client, "tools/list")).json()["result"]["tools"]}
    assert names == {"sniffer__catalog__search", "sniffer__catalog__guide", "vera__room__room_inbox"}
    assert calls[-1]["method"] == "tools/list" and calls[-1]["status"] == "ok"


async def test_tools_call_routes_to_upstream_with_original_name(hub, calls):
    client, fakes = hub
    r = await _rpc(client, "tools/call", {"name": "sniffer__catalog__search",
                                          "arguments": {"q": "honda"}})
    assert r.json()["result"]["content"][0]["text"] == 'called search {"q": "honda"}'
    assert calls[-1] | {"duration_ms": 0} == {
        "agent": "probe", "method": "tools/call", "tool": "sniffer__catalog__search",
        "project": "sniffer", "upstream": "catalog", "status": "ok", "duration_ms": 0, "error": ""}
    assert "honda" not in repr(calls), "аргументы в журнал не попадают"
    assert fakes["cat.example"].requests


@pytest.mark.parametrize("name", ["aib__video__generate", "sniffer__nope__x", "nonsense",
                                  "itstep__crm__anything"])
async def test_ungranted_missing_and_waiting_tools_look_the_same(hub, calls, name):
    client, fakes = hub
    r = await _rpc(client, "tools/call", {"name": name, "arguments": {}})
    assert r.json()["error"]["code"] == -32602
    assert r.json()["error"]["message"].startswith("unknown tool")
    assert calls[-1]["status"] == "denied"
    assert not fakes["vid.example"].requests, "вне прав апстрим не вызывается вовсе"


async def test_upstream_rpc_error_is_passed_through_without_secret(hub):
    client, _ = hub
    hub_fake = hub[1]["cat.example"]
    hub_fake.tools.append("boom")
    r = await _rpc(client, "tools/call", {"name": "sniffer__catalog__boom", "arguments": {}})
    assert r.json()["error"] == {"code": -32602, "message": "sniffer/catalog: bad args"}


async def test_secret_reaches_upstream_but_never_the_agent(hub, calls):
    client, fakes = hub
    r = await _rpc(client, "tools/call", {"name": "vera__room__room_inbox", "arguments": {}})
    sent = fakes["mcp"].requests[-1]
    listed = await _rpc(client, "tools/list")
    assert sent.headers["authorization"] == f"Bearer {SECRET}"
    assert sent.headers[AGENT_HEADER] == "probe", "внутреннему апстриму — имя агента"
    for text in (r.text, listed.text, repr(calls)):
        assert SECRET not in text


async def test_agent_name_is_not_sent_to_external_upstreams(hub):
    client, fakes = hub
    await _rpc(client, "tools/call", {"name": "sniffer__catalog__guide", "arguments": {}})
    assert AGENT_HEADER.lower() not in {k.lower() for k in fakes["cat.example"].requests[-1].headers}


async def test_auth_and_transport_guards(hub):
    client, _ = hub
    assert (await _rpc(client, "ping", token="wrong")).status_code == 401
    assert (await client.post(MCP_PATH, json={})).status_code == 401
    auth = {"Authorization": f"Bearer {TOKEN}"}
    assert (await client.get(MCP_PATH, headers=auth)).status_code == 405
    assert (await client.post(MCP_PATH, json={}, headers={
        **auth, "Origin": "https://evil.example"})).status_code == 403
    note = await client.post(MCP_PATH, headers=auth, json={
        "jsonrpc": "2.0", "method": "notifications/initialized"})
    assert note.status_code == 202
    batch = await client.post(MCP_PATH, headers=auth, json=[{"jsonrpc": "2.0", "id": 1,
                                                              "method": "ping"}])
    assert batch.json()["error"]["code"] == -32600
    bad = await client.post(MCP_PATH, headers=auth, content=b"{not json")
    assert bad.json()["error"]["code"] == -32700
    big = await client.post(MCP_PATH, headers=auth, content=b" " * (1024 * 1024 + 1))
    assert big.status_code == 413
    assert (await _rpc(client, "resources/list")).json()["error"]["code"] == -32601
    invalid = await client.post(MCP_PATH, headers=auth, json={"id": 1, "method": "ping"})
    assert invalid.json()["error"]["code"] == -32600


async def test_healthz_is_open(hub):
    client, _ = hub
    assert (await client.get("/healthz")).json() == {"status": "ok"}
