"""Общие заготовки тестов MCP-хаба: поддельный MCP-апстрим на httpx.MockTransport."""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from vera_shared.db.models_hub import HubUpstreamRow

HUB_KEY = "k" * 64
SECRET = "upstream-secret-DO-NOT-LEAK-0123456789"


def row(id_: int, project: str, slug: str, url: str, **kw: Any) -> HubUpstreamRow:
    base = {"transport": "http", "command": [], "auth_kind": "none", "auth_header": "",
            "secret_enc": None, "enabled": True, "timeout_s": 5, "max_concurrency": 4,
            "title": slug, "hint": ""}
    base.update(kw)
    return HubUpstreamRow(id=id_, project=project, slug=slug, url=url, **base)


class FakeUpstream:
    """MCP-сервер в памяти: initialize (с сессией), tools/list, tools/call, задержки и SSE."""

    def __init__(self, tools: list[str], *, session: bool = True, sse: bool = False,
                 delay: float = 0.0) -> None:
        self.tools, self.session, self.sse, self.delay = tools, session, sse, delay
        self.requests: list[httpx.Request] = []
        self.sessions_issued = 0
        self.expire_next = False

    def _reply(self, payload: dict[str, Any], headers: dict[str, str] | None = None) -> httpx.Response:
        if self.sse:
            body = f"event: message\ndata: {json.dumps(payload)}\n\n"
            return httpx.Response(200, text=body, headers={"content-type": "text/event-stream",
                                                            **(headers or {})})
        return httpx.Response(200, json=payload, headers=headers)

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        msg = json.loads(request.content)
        method, rid = msg.get("method"), msg.get("id")
        if rid is None:
            return httpx.Response(202)
        if method == "initialize":
            self.sessions_issued += 1
            headers = {"mcp-session-id": f"s{self.sessions_issued}"} if self.session else {}
            return self._reply({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake"}}}, headers)
        if self.expire_next:
            self.expire_next = False
            return httpx.Response(404)
        if method == "tools/list":
            return self._reply({"jsonrpc": "2.0", "id": rid, "result": {"tools": [
                {"name": t, "description": f"tool {t}", "inputSchema": {"type": "object"}}
                for t in self.tools]}})
        if method == "tools/call":
            name = msg["params"]["name"]
            if name == "boom":
                return self._reply({"jsonrpc": "2.0", "id": rid,
                                    "error": {"code": -32602, "message": "bad args"}})
            return self._reply({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": f"called {name} "
                             f"{json.dumps(msg['params'].get('arguments'))}"}]}})
        return self._reply({"jsonrpc": "2.0", "id": rid,
                            "error": {"code": -32601, "message": "nope"}})


def router(by_host: dict[str, FakeUpstream]) -> httpx.AsyncClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        fake = by_host.get(request.url.host)
        if fake is None:
            return httpx.Response(502)
        return await fake(request)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))
