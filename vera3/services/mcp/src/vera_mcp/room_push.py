"""Push-доставка новых сообщений комнаты: подписанный HTTPS-вебхук на каждое адресное сообщение.

Пуш — только сигнал. Курсор и история живут в БД, поэтому потерянный вебхук не теряет
сообщение: агент догонит его обычным `room_inbox`. Отсюда короткие попытки и без outbox.
Цели задаются `ROOM_PUSH_TARGETS=agent|https_url|whsec_secret` через запятую.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

from vera_mcp.events_net import (
    CallbackRejected,
    check_callback_url,
    post_pinned,
    signed_headers,
    validate_secret,
)

log = logging.getLogger(__name__)

PUSH_ATTEMPTS = 3
PUSH_BACKOFF_S = (1.0, 4.0)
EVENT_TYPE = "room.message"

_pending: set[asyncio.Task[None]] = set()
_cache: dict[str, PushTarget] | None = None


@dataclass(frozen=True)
class PushTarget:
    agent: str
    url: str
    secret: str


def parse_targets(raw: str) -> dict[str, PushTarget]:
    targets: dict[str, PushTarget] = {}
    for item in (part.strip() for part in raw.split(",")):
        if not item:
            continue
        parts = [part.strip() for part in item.split("|")]
        if len(parts) != 3 or not parts[0]:
            raise ValueError("ROOM_PUSH_TARGETS items must be agent|https_url|whsec_secret")
        agent, url, secret = parts
        if agent in targets:
            raise ValueError(f"ROOM_PUSH_TARGETS repeats agent {agent!r}")
        if not url.startswith("https://"):
            raise ValueError(f"ROOM_PUSH_TARGETS url for {agent!r} must be https")
        validate_secret(secret)
        targets[agent] = PushTarget(agent=agent, url=url, secret=secret)
    return targets


def _targets() -> dict[str, PushTarget]:
    global _cache
    if _cache is None:
        try:
            _cache = parse_targets(os.environ.get("ROOM_PUSH_TARGETS", ""))
        except ValueError as exc:
            log.error("room push disabled: %s", exc)
            _cache = {}
    return _cache


def schedule_push(message: dict[str, Any]) -> None:
    target = _targets().get(message.get("to") or "")
    if target is None or message["from"] == target.agent:
        return
    task = asyncio.get_running_loop().create_task(_deliver(target, message))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


def _payload(message: dict[str, Any]) -> str:
    return json.dumps({"type": EVENT_TYPE, "room": message["room"], "message": message},
                      ensure_ascii=False, separators=(",", ":"))


async def _deliver(target: PushTarget, message: dict[str, Any]) -> None:
    body = _payload(message)
    event_id = f"room-msg-{message['id']}"
    reason = "unknown"
    for attempt in range(PUSH_ATTEMPTS):
        try:
            addresses = await check_callback_url(target.url)
            headers = signed_headers(target.secret, event_id, body, f"room-push:{target.agent}")
            resp = await post_pinned(target.url, addresses, content=body.encode(),
                                     headers=headers)
        except CallbackRejected as exc:
            log.warning("room push rejected agent=%s reason=%s", target.agent, exc)
            return
        except httpx.HTTPError as exc:
            reason = type(exc).__name__
        else:
            if resp.status_code < 300:
                return
            reason = f"http_{resp.status_code}"
        if attempt < PUSH_ATTEMPTS - 1:
            await asyncio.sleep(PUSH_BACKOFF_S[attempt])
    log.warning("room push gave up agent=%s message=%s reason=%s",
                target.agent, message["id"], reason)
