"""MCP-хаб: клиенты апстримов, таймауты и лимиты, здоровье, stdio."""
from __future__ import annotations

import asyncio
import sys
import textwrap
import time

import httpx
import pytest
from hub_support import HUB_KEY, SECRET, FakeUpstream, router, row
from vera_hub.catalog import Catalog
from vera_hub.errors import UpstreamError
from vera_hub.health import check_all, interval_s
from vera_hub.upstream_http import HttpUpstream, _message_from_sse
from vera_hub.upstream_stdio import StdioUpstream
from vera_hub.upstreams import Registry, Upstream

pytestmark = pytest.mark.asyncio


def _registry(rows, fakes) -> Registry:
    async def loader():
        return rows
    return Registry(loader=loader, http=router(fakes))


async def test_hung_upstream_times_out_and_hub_keeps_serving(monkeypatch):
    slow, fast = FakeUpstream(["x"], delay=5), FakeUpstream(["y"])
    reg = _registry([row(1, "aib", "slow", "https://slow.example/", timeout_s=1),
                     row(2, "sniffer", "fast", "https://fast.example/")],
                    {"slow.example": slow, "fast.example": fast})
    ups = {u.slug: u for u in await reg.upstreams()}
    started = time.monotonic()
    with pytest.raises(UpstreamError) as exc:
        await reg.call(ups["slow"], "tools/list", {})
    assert exc.value.kind == "timeout"
    assert time.monotonic() - started < 3
    assert (await reg.call(ups["fast"], "tools/list", {}))["tools"][0]["name"] == "y"
    await reg.close()


async def test_concurrency_limit_answers_busy(monkeypatch):
    monkeypatch.setattr("vera_hub.upstreams.BUSY_WAIT_S", 0.1)
    slow = FakeUpstream(["x"], delay=1)
    reg = _registry([row(1, "aib", "slow", "https://slow.example/", max_concurrency=1)],
                    {"slow.example": slow})
    up = (await reg.upstreams())[0]
    first = asyncio.create_task(reg.call(up, "tools/list", {}))
    await asyncio.sleep(0.05)
    with pytest.raises(UpstreamError) as exc:
        await reg.call(up, "tools/list", {})
    assert exc.value.kind == "busy"
    await first
    await reg.close()


async def test_session_is_reused_and_reopened_after_404():
    fake = FakeUpstream(["a"])
    client = HttpUpstream(router({"u.example": fake}), "https://u.example/mcp", {})
    await client.request("tools/list", {})
    await client.request("tools/list", {})
    assert fake.sessions_issued == 1
    assert fake.requests[-1].headers["mcp-session-id"] == "s1"
    fake.expire_next = True
    await client.request("tools/list", {})
    assert fake.sessions_issued == 2


async def test_sse_reply_is_parsed():
    client = HttpUpstream(router({"u.example": FakeUpstream(["a"], sse=True)}), "https://u.example/", {})
    assert (await client.request("tools/list", {}))["tools"][0]["name"] == "a"
    assert _message_from_sse("data: not-json\n\n", 1) is None


@pytest.mark.parametrize(("status", "kind"), [(401, "down"), (500, "down")])
async def test_http_errors_become_upstream_errors(status, kind):
    async def handler(_request):
        return httpx.Response(status)
    client = HttpUpstream(httpx.AsyncClient(transport=httpx.MockTransport(handler)), "https://x/", {})
    with pytest.raises(UpstreamError) as exc:
        await client.request("tools/list", {})
    assert exc.value.kind == kind


async def test_unreachable_and_oversized_and_garbage(monkeypatch):
    async def refuse(request):
        raise httpx.ConnectError("refused", request=request)
    down = HttpUpstream(httpx.AsyncClient(transport=httpx.MockTransport(refuse)), "https://x/", {})
    with pytest.raises(UpstreamError, match="unreachable"):
        await down.request("tools/list", {})
    monkeypatch.setattr("vera_hub.upstream_http.MAX_BODY", 10)
    big = HttpUpstream(router({"u.example": FakeUpstream(["a"])}), "https://u.example/", {})
    with pytest.raises(UpstreamError, match="too large"):
        await big.request("tools/list", {})

    async def garbage(_request):
        return httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"})
    junk = HttpUpstream(httpx.AsyncClient(transport=httpx.MockTransport(garbage)), "https://x/", {})
    with pytest.raises(UpstreamError) as exc:
        await junk.request("tools/list", {})
    assert exc.value.kind == "protocol"


async def test_blockers_and_undecryptable_secret(monkeypatch):
    monkeypatch.setenv("HUB_SECRET_KEY", HUB_KEY)
    rows = [row(1, "itstep", "crm", "", auth_kind="url"),
            row(2, "personal", "tg", "", transport="stdio", enabled=False),
            row(3, "personal", "noconf", "", transport="stdio"),
            row(4, "aib", "broken", "https://b.example/", auth_kind="bearer", secret_enc="enc1:garbage")]
    reg = _registry(rows, {})
    ups = {u.slug: u for u in await reg.upstreams()}
    assert ups["crm"].blocker() == "needs_secret"
    assert ups["tg"].blocker() == "disabled"
    assert ups["noconf"].blocker() == "needs_config"
    assert SECRET not in repr(ups["broken"])
    with pytest.raises(UpstreamError, match="cannot be decrypted"):
        await reg.call(ups["broken"], "tools/list", {})
    with pytest.raises(UpstreamError, match="needs_secret"):
        await reg.call(ups["crm"], "tools/list", {})


async def test_health_pass_records_each_state(monkeypatch):
    rows = [row(1, "sniffer", "catalog", "https://c.example/"),
            row(2, "aib", "dead", "https://dead.example/"),
            row(3, "itstep", "crm", "", auth_kind="url")]
    reg = _registry(rows, {"c.example": FakeUpstream(["a", "b"])})
    seen: dict[int, tuple] = {}

    async def write(uid, health, error, tools):
        seen[uid] = (health, error, tools)
    await check_all(reg, Catalog(reg), write)
    assert seen[1] == ("ok", "", 2)
    assert seen[2][0] == "down" and seen[2][1].startswith("down:")
    assert seen[3] == ("needs_secret", "", None)
    assert interval_s() >= 15


async def test_catalog_paginates_and_keeps_stale_tools_on_failure():
    pages = {None: {"tools": [{"name": "a"}], "nextCursor": "p2"}, "p2": {"tools": [{"name": "b"}]}}

    class Paged:
        fail = False

        async def request(self, method, params, extra=None):
            if self.fail:
                raise UpstreamError("down", "gone")
            return pages[params.get("cursor")]

        async def close(self):
            return None

    reg = _registry([row(1, "sniffer", "catalog", "https://c.example/")], {})
    up = (await reg.upstreams())[0]
    fake = Paged()
    reg._clients[up.id] = (up, fake)
    catalog = Catalog(reg)
    assert [t["name"] for t in await catalog.refresh(up)] == ["a", "b"]
    fake.fail = True
    catalog._tools[up.id] = (up, 0.0, catalog._tools[up.id][2])
    names = [t["name"] for t in await catalog.tools_for(["sniffer"])]
    assert names == ["sniffer__catalog__a", "sniffer__catalog__b"]


_STDIO_SERVER = textwrap.dedent("""
    import json, os, sys
    for line in sys.stdin:
        msg = json.loads(line)
        if "id" not in msg:
            continue
        if msg["method"] == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {}}
        elif msg["method"] == "tools/list":
            result = {"tools": [{"name": "env_" + os.environ.get("FAKE_TOKEN", "none")[:4]}]}
        elif msg["method"] == "tools/call" and msg["params"]["name"] == "exit":
            sys.exit(0)
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                              "error": {"code": -32601, "message": "nope"}}), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
""")


async def test_stdio_upstream_round_trip_and_restart(tmp_path):
    script = tmp_path / "server.py"
    script.write_text(_STDIO_SERVER, encoding="utf-8")
    client = StdioUpstream([sys.executable, str(script)], {"FAKE_TOKEN": "abcdef"})
    try:
        assert (await client.request("tools/list", {}))["tools"][0]["name"] == "env_abcd"
        with pytest.raises(UpstreamError) as exc:
            await client.request("tools/call", {"name": "other"})
        assert exc.value.kind == "rpc"
        with pytest.raises(UpstreamError, match="exited|not running|closed"):
            await asyncio.wait_for(client.request("tools/call", {"name": "exit"}), 10)
        assert (await client.request("tools/list", {}))["tools"][0]["name"] == "env_abcd"
    finally:
        await client.close()


async def test_stdio_missing_binary_is_down():
    client = StdioUpstream(["/definitely/not/here"], {})
    with pytest.raises(UpstreamError, match="failed to start"):
        await client.request("tools/list", {})


async def test_upstream_repr_hides_secret():
    up = Upstream.of(row(1, "aib", "video", "https://v/", auth_kind="bearer", secret_enc="enc1:zzz"))
    assert "enc1" not in repr(up)
