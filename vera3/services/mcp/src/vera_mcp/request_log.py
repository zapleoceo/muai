"""Журнал запросов /mcp: одна строка на запрос, чтобы сопоставить отмену на стороне клиента.

Чистый ASGI (не BaseHTTPMiddleware) — стримы и отмена не ломаются. В лог идут только
метаданные: метод JSON-RPC и имя инструмента, но не params/arguments/тела.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from vera_mcp.auth import CLIENT_SCOPE_KEY

log = logging.getLogger("vera_mcp.request")

MCP_PATH = "/mcp"
REQUEST_ID_HEADER = b"x-request-id"
CORRELATION_HEADER = b"x-correlation-id"
BODY_PEEK_LIMIT = 64 * 1024
_SANE_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
_UNSAFE = re.compile(r"[^A-Za-z0-9._:/-]")


def pick_cid(headers: list[tuple[bytes, bytes]]) -> str:
    found = {k.lower(): v.decode("latin-1") for k, v in headers}
    for key in (REQUEST_ID_HEADER, CORRELATION_HEADER):
        value = found.get(key, "")
        if _SANE_ID.fullmatch(value):
            return value
    return uuid.uuid4().hex[:16]


def rpc_summary(body: bytes) -> tuple[str, str, str]:
    """(method, tool, id) из JSON-RPC; пустые строки, если разобрать нельзя."""
    try:
        data: Any = json.loads(body)
    except (ValueError, RecursionError):
        return "", "", ""
    if isinstance(data, list):
        data = next((d for d in data if isinstance(d, dict)), None)
    if not isinstance(data, dict):
        return "", "", ""
    method = data.get("method")
    params = data.get("params")
    tool = ""
    if method == "tools/call" and isinstance(params, dict) and isinstance(params.get("name"), str):
        tool = params["name"]
    rid = data.get("id")
    return (method if isinstance(method, str) else "", tool,
            "" if rid is None or isinstance(rid, (dict, list)) else str(rid))


def _clean(value: str, limit: int) -> str:
    return _UNSAFE.sub("_", value)[:limit]


async def _peek_body(receive: Receive) -> tuple[list[Message], bytes]:
    messages: list[Message] = []
    chunks: list[bytes] = []
    size = 0
    while size < BODY_PEEK_LIMIT:
        msg = await receive()
        messages.append(msg)
        if msg["type"] != "http.request":
            return messages, b""
        chunks.append(msg.get("body", b""))
        size += len(chunks[-1])
        if not msg.get("more_body"):
            return messages, b"".join(chunks)
    return messages, b""


class RequestLogMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != MCP_PATH:
            await self.app(scope, receive, send)
            return
        headers = scope.get("headers", [])
        cid = pick_cid(headers)
        method = scope.get("method", "")
        started = time.perf_counter()
        log.debug("mcp_start cid=%s method=%s", cid, method)

        replay: list[Message] = []
        peeked = b""
        if method == "POST":
            replay, peeked = await _peek_body(receive)
        state = {"status": 0, "disconnected": False, "complete": False}

        async def wrapped_receive() -> Message:
            msg = replay.pop(0) if replay else await receive()
            if msg["type"] == "http.disconnect":
                state["disconnected"] = True
            return msg

        async def wrapped_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
                message = {**message, "headers": [
                    *(h for h in message.get("headers", []) if h[0].lower() != REQUEST_ID_HEADER),
                    (REQUEST_ID_HEADER, cid.encode())]}
            elif message["type"] == "http.response.body" and not message.get("more_body"):
                state["complete"] = True
            await send(message)

        outcome = "ok"
        try:
            await self.app(scope, wrapped_receive, wrapped_send)
        except Exception:
            outcome = "server_exception"
            state["status"] = state["status"] or 500
            raise
        except BaseException:
            outcome = "aborted"
            raise
        else:
            if state["disconnected"] and not state["complete"]:
                outcome = "client_disconnected"
            elif state["status"] >= 400:
                outcome = "http_error"
        finally:
            rpc_method, tool, rid = rpc_summary(peeked)
            ua = next((v.decode("latin-1") for k, v in headers if k.lower() == b"user-agent"), "")
            log.info(
                "mcp_request cid=%s method=%s path=%s actor=%s ua=%s rpc=%s tool=%s "
                "rpc_id=%s status=%s ms=%d outcome=%s",
                cid, method, MCP_PATH, _clean(str(scope.get(CLIENT_SCOPE_KEY, "-")), 64),
                _clean(ua, 80) or "-", _clean(rpc_method, 64) or "-", _clean(tool, 64) or "-",
                _clean(rid, 32) or "-", state["status"],
                (time.perf_counter() - started) * 1000, outcome)
