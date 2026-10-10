"""Transactional outbox MCP Events: строка доставки пишется в транзакции события задачи."""
from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.models_room import RoomTaskEventRow
from vera_shared.db.models_room_events import RoomEventOutboxRow, RoomEventSubscriptionRow
from vera_shared.timeutil import utc_naive_now

EVENT_NAMESPACE = uuid.UUID("6f1c3a52-0b7e-5d4f-9a31-7e2b8c4d1f90")

# kind журнала задачи -> имя события MCP Events
EVENT_NAME_BY_KIND: dict[str, str] = {
    "created": "ready", "released": "ready", "unblocked": "ready",
    "lease_expired": "ready", "resumed": "ready",
    "done": "result", "review": "result",
    "blocked": "blocker", "question": "blocker", "waiting": "blocker",
}


def event_id_for(room: str, task_id: str, revision: int, kind: str) -> str:
    return str(uuid.uuid5(EVENT_NAMESPACE, f"{room}/{task_id}/{revision}/{kind}"))


def build_body(*, event_id: str, name: str, row: RoomTaskEventRow, revision: int,
               causation_id: str | None) -> str:
    stamp = utc_naive_now().isoformat(timespec="seconds") + "Z"
    data = {"task_id": row.task_id, "room": row.room, "revision": revision, "kind": row.kind,
            "correlation_id": f"{row.room}/{row.task_id}", "causation_id": causation_id,
            "actor": row.agent or "system"}
    return json.dumps({"eventId": event_id, "name": name, "timestamp": stamp, "data": data,
                       "cursor": None}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _matches(sub: RoomEventSubscriptionRow, row: RoomTaskEventRow) -> bool:
    return ((sub.task_id is None or sub.task_id == row.task_id)
            and (not sub.kinds or row.kind in sub.kinds))


async def _previous(db: AsyncSession, sub_id: str, task_id: str) -> RoomEventOutboxRow | None:
    q = (select(RoomEventOutboxRow)
         .where(RoomEventOutboxRow.subscription_id == sub_id, RoomEventOutboxRow.task_id == task_id)
         .order_by(RoomEventOutboxRow.id.desc()).limit(1))
    return (await db.execute(q)).scalar_one_or_none()


async def _exists(db: AsyncSession, sub_id: str, event_id: str) -> bool:
    q = select(RoomEventOutboxRow.id).where(RoomEventOutboxRow.subscription_id == sub_id,
                                            RoomEventOutboxRow.event_id == event_id)
    return (await db.execute(q)).first() is not None


async def enqueue_task_event(db: AsyncSession, row: RoomTaskEventRow) -> list[RoomEventOutboxRow]:
    """Вызывается из record_event: откат транзакции откатывает и outbox.

    Самопетля: актор-подписчик реагирует на своё пробуждение (причина — событие, ранее
    поставленное этой же подписке) и снова порождает событие того же имени — такое
    событие не ставится. Результат или блокер от того же актора доставляются.
    """
    name = EVENT_NAME_BY_KIND.get(row.kind)
    if name is None:
        return []
    revision, now, actor = row.id, utc_naive_now(), row.agent or "system"
    q = select(RoomEventSubscriptionRow).where(
        RoomEventSubscriptionRow.room == row.room, RoomEventSubscriptionRow.event_name == name,
        RoomEventSubscriptionRow.status == "active", RoomEventSubscriptionRow.expires_at > now)
    event_id = event_id_for(row.room, row.task_id, revision, row.kind)
    queued: list[RoomEventOutboxRow] = []
    for sub in (await db.execute(q)).scalars():
        if not _matches(sub, row) or await _exists(db, sub.id, event_id):
            continue
        prev = await _previous(db, sub.id, row.task_id) if sub.principal == actor else None
        if prev is not None and prev.event_name == name:
            continue
        out = RoomEventOutboxRow(
            subscription_id=sub.id, event_id=event_id, event_name=name, task_id=row.task_id,
            body=build_body(event_id=event_id, name=name, row=row, revision=revision,
                            causation_id=prev.event_id if prev else None),
            source_event_id=row.id, next_attempt_at=now)
        db.add(out)
        queued.append(out)
    await db.flush()
    return queued


def parse_body(body: str) -> dict[str, Any]:
    return json.loads(body)


TERMINAL_STATUSES = ("delivered", "dropped", "failed", "revoked")


async def prune_outbox(db: AsyncSession, *, older_than_days: int = 30) -> int:
    """Удаляет завершённые строки старше N суток; pending не трогает. Пока не подключено к циклу."""
    cutoff = utc_naive_now() - timedelta(days=older_than_days)
    res = await db.execute(delete(RoomEventOutboxRow).where(
        RoomEventOutboxRow.status.in_(TERMINAL_STATUSES), RoomEventOutboxRow.created_at < cutoff))
    return int(res.rowcount or 0)
