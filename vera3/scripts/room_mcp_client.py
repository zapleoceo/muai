"""Минимальный MCP-клиент на стандартной библиотеке: initialize → tools/call.

Streamable HTTP отвечает либо JSON, либо SSE (`data: {...}`) — разбираются оба.
"""
from __future__ import annotations

import json
import urllib.request
from typing import Any

PROTOCOL_VERSION = "2025-06-18"


class ToolError(RuntimeError):
    """Инструмент вернул isError или JSON-RPC error."""


def parse_body(raw: bytes) -> dict[str, Any]:
    text = raw.decode("utf-8").strip()
    if text.startswith("{"):
        return json.loads(text)
    found: dict[str, Any] | None = None
    for line in text.splitlines():
        if line.startswith("data:"):
            chunk = line[5:].strip()
            if chunk:
                found = json.loads(chunk)
    if found is None:
        raise ValueError("MCP response has no JSON payload")
    return found


def _rpc(url: str, token: str, payload: dict[str, Any], session: str | None,
         timeout: float) -> tuple[str | None, bytes]:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream",
               "MCP-Protocol-Version": PROTOCOL_VERSION}
    if session:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers,
                                 method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.headers.get("Mcp-Session-Id"), resp.read()


def call_tool(url: str, token: str, name: str, arguments: dict[str, Any],
              timeout: float = 30.0) -> dict[str, Any]:
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
        "clientInfo": {"name": "room-listener", "version": "2"}}}
    session, _ = _rpc(url, token, init, None, 30.0)
    _rpc(url, token, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session, 30.0)
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}
    _, raw = _rpc(url, token, call, session, timeout)
    reply = parse_body(raw)
    if "error" in reply:
        raise ToolError(str(reply["error"].get("message", reply["error"])))
    result = reply["result"]
    if result.get("isError"):
        texts = [c.get("text", "") for c in result.get("content", [])]
        raise ToolError(" ".join(texts) or "tool error")
    if isinstance(result.get("structuredContent"), dict):
        return result["structuredContent"]
    return json.loads(result["content"][0]["text"])
