"""Каталог MCP Events: какие события отдаёт комната и как они описаны в events/list."""
from __future__ import annotations

import json
from typing import Any

from mcp import types
from mcp.server.context import ServerRequestContext
from starlette.types import ASGIApp, Message, Receive, Scope, Send

EVENTS_PROTOCOL = "2026-07-28"
TASK_KINDS = ("ready", "result", "blocker")

_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "room": {"type": "string", "description": "Комната; только доступные токену"},
        "task_id": {"type": "string", "description": "Ограничить одной задачей"},
        "kind": {"type": "array", "items": {"type": "string"},
                 "description": "Типы событий задачи в комнате (kind из истории задачи)"},
    },
    "required": ["room"],
    "additionalProperties": False,
}

_PAYLOAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string"},
        "room": {"type": "string"},
        "revision": {"type": "integer", "description": "Версия задачи; событие с revision "
                     "меньше текущей приёмник игнорирует"},
        "kind": {"type": "string"},
        "correlation_id": {"type": "string"},
        "causation_id": {"type": ["string", "null"]},
        "actor": {"type": "string"},
    },
    "required": ["task_id", "room", "revision", "kind", "correlation_id", "causation_id", "actor"],
    "additionalProperties": False,
}

_DESCRIPTIONS = {
    "ready": "Задача открыта или снова свободна: её можно брать в работу.",
    "result": "По задаче есть результат.",
    "blocker": "Задача заблокирована или ждёт ответа владельца.",
}


def is_stale_event(event_revision: int, current_revision: int) -> bool:
    """Приёмник: порядок доставки не гарантирован — событие с revision ниже текущей игнорируется."""
    return event_revision < current_revision


def event_definitions() -> list[dict[str, Any]]:
    return [{"name": name, "description": _DESCRIPTIONS[name], "delivery": ["webhook"],
             "inputSchema": _INPUT_SCHEMA, "payloadSchema": _PAYLOAD_SCHEMA}
            for name in TASK_KINDS]


async def list_events(_ctx: ServerRequestContext, _params: types.PaginatedRequestParams) -> dict[str, Any]:
    return {"events": event_definitions(), "resultType": "complete"}


class AdvertiseEvents:
    """ASGI-обёртка: добавляет `capabilities.events` в ответ `server/discover`.

    SDK 2.3 отдаёт результат через строгую схему ServerCapabilities (extra=ignore),
    и верхнеуровневый `events` из хендлера в ответ не попадает.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        headers = dict(scope.get("headers", [])) if scope["type"] == "http" else {}
        if headers.get(b"mcp-method") != b"server/discover":
            await self.app(scope, receive, send)
            return
        start: Message = {}
        chunks: list[bytes] = []

        async def capture(message: Message) -> None:
            if message["type"] == "http.response.start":
                start.update(message)
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))

        await self.app(scope, receive, capture)
        body = _with_events(b"".join(chunks))
        out = [(k, v) for k, v in start.get("headers", []) if k.lower() != b"content-length"]
        out.append((b"content-length", str(len(body)).encode()))
        await send({**start, "headers": out})
        await send({"type": "http.response.body", "body": body})


def _with_events(raw: bytes) -> bytes:
    try:
        doc = json.loads(raw)
        doc["result"]["capabilities"]["events"] = {}
    except (ValueError, KeyError, TypeError):
        return raw
    return json.dumps(doc, ensure_ascii=False).encode()
