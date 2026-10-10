"""Общие заглушки MCP Events: локальный ASGI-«получатель» и вызов JSON-RPC по проводу."""
from __future__ import annotations

import base64
import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
from standardwebhooks import Webhook
from vera_mcp.server import build_app

MODERN = "2026-07-28"
ENVELOPE = {"io.modelcontextprotocol/protocolVersion": MODERN,
            "io.modelcontextprotocol/clientCapabilities": {}}
TOKENS = {"claude": "room-claude-0123456789-0123456789", "codex": "room-codex-abcdefghij-abcdefghij"}
HOST = "hooks.example.test"
URL = f"https://{HOST}/cb"


def new_secret() -> str:
    return "whsec_" + base64.b64encode(os.urandom(32)).decode()


@dataclass
class FakeReceiver:
    """Записывает запросы; verification отвечает эхом, события — заданным статусом."""
    secret: str
    mode: str = "echo"
    statuses: list[int] = field(default_factory=lambda: [200])
    requests: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(self, scope, receive, send) -> None:
        chunks = []
        while True:
            msg = await receive()
            chunks.append(msg.get("body", b""))
            if not msg.get("more_body"):
                break
        body = b"".join(chunks)
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        verified = True
        try:
            Webhook(self.secret).verify(body, headers)
        except Exception:
            verified = False
        self.requests.append({"headers": headers, "body": body, "verified": verified,
                              "json": json.loads(body)})
        payload = json.loads(body)
        if payload.get("type") == "verification" and self.mode == "redirect":
            await send({"type": "http.response.start", "status": 302,
                        "headers": [(b"location", b"https://other.example.test/cb")]})
            await send({"type": "http.response.body", "body": b""})
            return
        if payload.get("type") == "verification":
            status, out = (200, {"challenge": payload["challenge"]}) if self.mode == "echo" else (
                200, {"challenge": "wrong"})
        else:
            status = self.statuses[min(len(self.events) - 1, len(self.statuses) - 1)]
            out = {}
        data = json.dumps(out).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": data})

    @property
    def events(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["json"].get("type") != "verification"]


@asynccontextmanager
async def open_client():
    app = build_app()
    async with (app.router.lifespan_context(app),
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c):
        yield c


async def rpc(c: httpx.AsyncClient, who: str, method: str, params: dict | None = None) -> dict:
    body = {"jsonrpc": "2.0", "id": 1, "method": method,
            "params": {**(params or {}), "_meta": ENVELOPE}}
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
               "Authorization": f"Bearer {TOKENS[who]}", "MCP-Protocol-Version": MODERN,
               "Mcp-Method": method}
    return (await c.post("/mcp", json=body, headers=headers)).json()


def subscribe_params(secret: str, url: str = URL, name: str = "ready", **arguments: Any) -> dict:
    return {"name": name, "arguments": {"room": "main", **arguments},
            "delivery": {"mode": "webhook", "url": url, "secret": secret}, "cursor": None}
