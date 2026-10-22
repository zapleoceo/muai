"""Доставка MCP Events из outbox: подпись на каждую попытку, backoff, 410/413 без повторов.

Функция `deliver_due` вызывается из существующего poll-цикла; сама ничего не запускает.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
from collections.abc import Callable
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select, update
from vera_shared.crypto import decrypt
from vera_shared.db.engine import get_session
from vera_shared.db.models_room_events import (
    RoomEventOutboxRow,
    RoomEventSubscriptionRow,
)
from vera_shared.timeutil import utc_naive_now

from vera_mcp.auth import allowed_rooms
from vera_mcp.events_net import (
    MAX_BODY_BYTES,
    CallbackRejected,
    check_callback_url,
    post_pinned,
    signed_headers,
)
from vera_mcp.room_oauth import configured_agents

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 6
BASE_DELAY_S = 5.0
MAX_DELAY_S = 900.0
MAX_CONCURRENCY = 5
ATTEMPT_DEADLINE_S = 20.0


def backoff_delay(attempt: int, rand: Callable[[], float] = random.random) -> float:
    """Экспоненциальная задержка с потолком и джиттером 50..100%."""
    return min(MAX_DELAY_S, BASE_DELAY_S * 2 ** max(attempt - 1, 0)) * (0.5 + rand() / 2)


def _close(row: RoomEventOutboxRow, status: str, error: str | None, now: datetime) -> None:
    row.status, row.last_error = status, error
    row.delivered_at = now if status == "delivered" else None


async def _revoke_all(sub_id: str) -> None:
    async with get_session() as s:
        await s.execute(update(RoomEventSubscriptionRow)
                        .where(RoomEventSubscriptionRow.id == sub_id).values(status="revoked"))
        await s.execute(update(RoomEventOutboxRow)
                        .where(RoomEventOutboxRow.subscription_id == sub_id,
                               RoomEventOutboxRow.status == "pending").values(status="revoked"))


def principal_still_authorised(principal: str, room: str) -> bool:
    """Токен/актор принципала ещё существует и комната по-прежнему разрешена."""
    return principal in configured_agents() and room in allowed_rooms()


async def _post(sub: RoomEventSubscriptionRow, row: RoomEventOutboxRow,
                addresses: list[str]) -> int | str:
    """HTTP-статус или короткий код сетевой ошибки; тела и URL в логи не попадают."""
    secret = decrypt(sub.encrypted_secret, secret=os.environ["ROOM_OAUTH_KEY"])
    headers = signed_headers(secret, row.event_id, row.body, sub.id)
    try:
        resp = await post_pinned(sub.callback_url, addresses, content=row.body.encode(),
                                 headers=headers)
    except httpx.TimeoutException:
        return "timeout"
    except httpx.HTTPError:
        return "network"
    return resp.status_code


async def _attempt(row_id: int, now: datetime) -> None:
    outcome: int | str | None = None
    sub_id = ""
    async with get_session() as s:
        row = await s.get(RoomEventOutboxRow, row_id)
        if row is None or row.status != "pending":
            return
        sub_id = row.subscription_id
        sub = await s.get(RoomEventSubscriptionRow, sub_id)
        if sub is None or sub.status != "active" or (sub.expires_at and sub.expires_at <= now):
            _close(row, "revoked", "subscription_inactive", now)
            return
        if len(row.body.encode()) > MAX_BODY_BYTES:
            _close(row, "dropped", "body_too_large", now)
            return
        if not principal_still_authorised(sub.principal, sub.room):
            sub.status = "revoked"
            _close(row, "revoked", "principal_not_authorised", now)
            await s.execute(update(RoomEventOutboxRow).where(
                RoomEventOutboxRow.subscription_id == sub.id, RoomEventOutboxRow.status == "pending",
                RoomEventOutboxRow.id != row.id).values(status="revoked"))
            return
        try:
            addresses = await check_callback_url(sub.callback_url)
        except CallbackRejected as exc:
            _close(row, "dropped", exc.reason, now)
            return
        outcome = await _post(sub, row, addresses)
        row.attempts += 1
        if isinstance(outcome, int) and 200 <= outcome < 300:
            _close(row, "delivered", None, now)
        elif outcome == 413:
            _close(row, "dropped", "http_413", now)
        elif outcome == 410:
            _close(row, "revoked", "http_410", now)
        elif row.attempts >= MAX_ATTEMPTS:
            _close(row, "failed", f"http_{outcome}" if isinstance(outcome, int) else str(outcome), now)
        else:
            row.last_error = f"http_{outcome}" if isinstance(outcome, int) else str(outcome)
            row.next_attempt_at = now + timedelta(seconds=backoff_delay(row.attempts))
        log.info("mcp event %s sub=%s attempt=%s outcome=%s", row.event_id, sub.id, row.attempts, outcome)
    if outcome == 410:
        await _revoke_all(sub_id)


async def deliver_due(*, now: datetime | None = None, limit: int = 50) -> int:
    """Одна итерация: берёт созревшие pending-строки и делает по одной попытке. Возвращает число попыток."""
    moment = now or utc_naive_now()
    async with get_session() as s:
        q = (select(RoomEventOutboxRow.id, RoomEventOutboxRow.subscription_id)
             .where(RoomEventOutboxRow.status == "pending", RoomEventOutboxRow.next_attempt_at <= moment)
             .order_by(RoomEventOutboxRow.id).limit(limit))
        due = [(r[0], r[1]) for r in (await s.execute(q)).all()]
    gate = asyncio.Semaphore(MAX_CONCURRENCY)
    # строки одной подписки идут строго по очереди: 410 на первой гасит остальные до их попытки
    per_sub = {sub_id: asyncio.Lock() for _, sub_id in due}

    async def bounded(row_id: int, sub_id: str) -> None:
        async with per_sub[sub_id], gate:
            try:
                await asyncio.wait_for(_attempt(row_id, moment), ATTEMPT_DEADLINE_S)
            except TimeoutError:
                log.warning("mcp event delivery row=%s exceeded %ss deadline", row_id, ATTEMPT_DEADLINE_S)

    await asyncio.gather(*(bounded(i, sub_id) for i, sub_id in due))
    return len(due)
