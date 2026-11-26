"""Клиент MCP Streamable HTTP к одному апстриму.

Сессия (`Mcp-Session-Id`) поднимается лениво и переживает вызовы; 404 на
сессию — один повтор с новым initialize. Ответ апстрима может быть JSON или
SSE-потоком: из потока берётся сообщение с нашим id. Тело ответа ограничено
MAX_BODY, чтобы один болтливый апстрим не съел память хаба.
"""
from __future__ import annotations

import asyncio
import itertools
import json
from dataclasses import dataclass
from typing import Any

import httpx

from vera_hub.errors import UpstreamError

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "vera-hub", "version": "1.0"}
MAX_BODY = 8 * 1024 * 1024
_ids = itertools.count(1)


def _message_from_sse(text: str, rid: int) -> dict[str, Any] | None:
    for event in text.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(line[5:].lstrip() for line in event.split("\n") if line.startswith("data:"))
        if not data:
            continue
        try:
            msg = json.loads(data)
        except ValueError:
            continue
        if isinstance(msg, dict) and msg.get("id") == rid:
            return msg
    return None


@dataclass
class _Reply:
    status: int
    headers: httpx.Headers
    body: bytes


class HttpUpstream:
    def __init__(self, client: httpx.AsyncClient, url: str, headers: dict[str, str]) -> None:
        self._client, self._url, self._headers = client, url, headers
        self._session: str | None = None
        self._version = PROTOCOL_VERSION
        self._ready = False
        self._init_lock = asyncio.Lock()

    def _request_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Accept": "application/json, text/event-stream",
                   "Content-Type": "application/json", **(extra or {}), **self._headers}
        if self._ready:
            headers["MCP-Protocol-Version"] = self._version
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        return headers

    async def _post(self, payload: dict[str, Any],
                    extra: dict[str, str] | None = None) -> _Reply:
        try:
            async with self._client.stream("POST", self._url, json=payload,
                                           headers=self._request_headers(extra)) as resp:
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_BODY:
                        raise UpstreamError("protocol", "upstream response too large")
                return _Reply(resp.status_code, resp.headers, bytes(body))
        except httpx.TimeoutException as exc:
            raise UpstreamError("timeout", "upstream timed out") from exc
        except httpx.HTTPError as exc:
            raise UpstreamError("down", f"upstream unreachable: {type(exc).__name__}") from exc

    async def _rpc(self, method: str, params: dict[str, Any],
                   extra: dict[str, str] | None = None) -> dict[str, Any]:
        rid = next(_ids)
        resp = await self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params},
                                extra)
        if resp.status == 404 and self._session:
            raise _SessionExpired
        if resp.status in (401, 403):
            raise UpstreamError("down", f"upstream refused credentials ({resp.status})")
        if resp.status >= 400:
            raise UpstreamError("down", f"upstream HTTP {resp.status}")
        if method == "initialize" and resp.headers.get("mcp-session-id"):
            self._session = resp.headers["mcp-session-id"]
        msg = self._parse(resp, rid)
        if "error" in msg:
            err = msg["error"] if isinstance(msg["error"], dict) else {}
            raise UpstreamError("rpc", str(err.get("message", "upstream error")),
                                int(err.get("code", -32000)))
        result = msg.get("result")
        if not isinstance(result, dict):
            raise UpstreamError("protocol", "upstream result is not an object")
        return result

    @staticmethod
    def _parse(resp: _Reply, rid: int) -> dict[str, Any]:
        ctype = resp.headers.get("content-type", "")
        try:
            msg = (_message_from_sse(resp.body.decode("utf-8", "replace"), rid)
                   if "text/event-stream" in ctype else json.loads(resp.body))
        except ValueError as exc:
            raise UpstreamError("protocol", "upstream sent invalid JSON") from exc
        if not isinstance(msg, dict):
            raise UpstreamError("protocol", "upstream sent no JSON-RPC response")
        return msg

    async def _ensure_ready(self) -> None:
        async with self._init_lock:
            if self._ready:
                return
            self._session = None
            result = await self._rpc("initialize", {
                "protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO})
            self._version = str(result.get("protocolVersion") or PROTOCOL_VERSION)
            self._ready = True
            await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def request(self, method: str, params: dict[str, Any],
                      extra: dict[str, str] | None = None) -> dict[str, Any]:
        for attempt in (1, 2):
            await self._ensure_ready()
            try:
                return await self._rpc(method, params, extra)
            except _SessionExpired:
                self._ready = False
                if attempt == 2:
                    raise UpstreamError("down", "upstream keeps dropping the session") from None
        raise AssertionError("unreachable")

    async def close(self) -> None:
        self._ready = False


class _SessionExpired(Exception):
    pass
