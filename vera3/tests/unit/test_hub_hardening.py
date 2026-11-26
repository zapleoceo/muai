"""MCP-хаб: находки независимого ревью — редиректы, логи, stdio-читатель, встречные запросы."""
from __future__ import annotations

import logging
import sys
import textwrap

import httpx
import pytest
from hub_support import HUB_KEY, SECRET, row
from vera_hub.auth import CACHE_LIMIT, AgentAuth
from vera_hub.errors import UpstreamError
from vera_hub.rpc import HubRpc
from vera_hub.upstream_http import HttpUpstream, is_reply
from vera_hub.upstream_stdio import StdioUpstream
from vera_hub.upstreams import AGENT_HEADER, Registry

pytestmark = pytest.mark.asyncio


async def test_redirect_is_not_followed_and_secret_header_stays_home():
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(307, headers={"location": "https://evil.example/steal"})

    reg = Registry(loader=None, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    client = HttpUpstream(reg._http, "https://up.example/mcp", {"X-Api-Key": SECRET})
    with pytest.raises(UpstreamError, match="redirected"):
        await client.request("tools/list", {})
    assert {r.url.host for r in seen} == {"up.example"}
    await reg.close()


async def test_default_client_does_not_follow_redirects():
    reg = Registry(loader=None)
    assert reg._http.follow_redirects is False
    assert reg._http.timeout.pool == 3.0
    await reg.close()


async def test_url_secret_never_reaches_logs(monkeypatch, caplog):
    import vera_hub.__main__ as entry
    monkeypatch.setenv("HUB_SECRET_KEY", HUB_KEY)
    monkeypatch.setattr(entry.uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setattr(entry, "build_app", lambda: None)
    entry.main()
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING

    async def ok(_request):
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})
    caplog.set_level(logging.INFO)
    url = f"https://crm.example/mcp/crm?token={SECRET}"
    async with httpx.AsyncClient(transport=httpx.MockTransport(ok)) as http:
        await http.post(url, json={})
    assert SECRET not in caplog.text


async def test_agent_header_follows_the_real_url(monkeypatch):
    from vera_shared.hub.secrets import seal
    monkeypatch.setenv("HUB_SECRET_KEY", HUB_KEY)
    seen: list[httpx.Request] = []

    async def handler(request):
        seen.append(request)
        import json
        rid = json.loads(request.content).get("id")
        if rid is None:
            return httpx.Response(202)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": {"tools": []}})

    rows = [row(1, "itstep", "crm", "http://mcp:8000/display", auth_kind="url",
                secret_enc=seal("https://external.example/mcp?t=x"))]

    async def loader():
        return rows
    reg = Registry(loader=loader, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await reg.call((await reg.upstreams())[0], "tools/list", {}, agent="probe")
    assert seen[-1].url.host == "external.example"
    assert AGENT_HEADER.lower() not in {k.lower() for k in seen[-1].headers}
    await reg.close()


def test_server_requests_are_not_replies():
    assert is_reply({"jsonrpc": "2.0", "id": 1, "result": {}}, 1)
    assert not is_reply({"jsonrpc": "2.0", "id": 1, "method": "ping"}, 1)
    assert not is_reply({"jsonrpc": "2.0", "id": 1}, 1)
    assert not is_reply({"jsonrpc": "2.0", "id": 2, "result": {}}, 1)


async def test_client_jsonrpc_response_gets_202():
    rpc = HubRpc(registry=None, catalog=None)
    agent = type("A", (), {"name": "probe", "allow": ()})()
    assert await rpc.handle(agent, {"jsonrpc": "2.0", "id": 5, "result": {}}) is None


async def test_miss_flood_does_not_evict_real_agents():
    class Row:
        id, name, allow = 1, "probe", []
    lookups: list[str] = []

    async def lookup(token):
        lookups.append(token)
        return Row() if token == "good" else None

    async def touch(_id):
        return None
    auth = AgentAuth(lookup, touch)
    await auth.resolve("Bearer good")
    for i in range(CACHE_LIMIT + 10):
        await auth.resolve(f"Bearer junk{i}")
    await auth.resolve("Bearer good")
    assert lookups.count("good") == 1


_PINGY = textwrap.dedent("""
    import json, sys
    for line in sys.stdin:
        msg = json.loads(line)
        if "id" not in msg or "method" not in msg:
            continue
        if msg["method"] == "initialize":
            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "method": "ping"}), flush=True)
            print(json.dumps({"jsonrpc": "2.0", "id": [1], "result": {}}), flush=True)
            result = {"protocolVersion": "2025-06-18", "capabilities": {}}
        elif msg["params"].get("name") == "huge":
            print("x" * 200, flush=True)
            continue
        else:
            result = {"tools": [{"name": "ok"}]}
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
""")


async def test_stdio_answers_pings_and_restarts_after_oversized_line(tmp_path, monkeypatch):
    monkeypatch.setattr("vera_hub.upstream_stdio.MAX_LINE", 100)
    script = tmp_path / "pingy.py"
    script.write_text(_PINGY, encoding="utf-8")
    client = StdioUpstream([sys.executable, str(script)], {})
    try:
        assert (await client.request("tools/list", {}))["tools"][0]["name"] == "ok"
        with pytest.raises(UpstreamError):
            await client.request("tools/call", {"name": "huge"})
        assert (await client.request("tools/list", {}))["tools"][0]["name"] == "ok"
    finally:
        await client.close()
