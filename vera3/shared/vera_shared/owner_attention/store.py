"""Хранение состояния внимания и очереди отложенного (репозиторный слой)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models_attention import OwnerAttentionRow, OwnerDeferredRow
from vera_shared.db.models_room import RoomTaskQuestionRow, RoomTaskRow

ACTIVE, WAIT10, QUIET = "active", "wait10", "quiet"
CLOSED_TASK = ("done", "cancelled")


@dataclass(frozen=True)
class Attention:
    state: str = ACTIVE
    wait_question_ref: str | None = None
    wait_deadline: datetime | None = None
    quiet_until: datetime | None = None


@dataclass(frozen=True)
class Deferred:
    dedup_key: str
    kind: str
    text: str
    created_at: datetime
    room: str | None = None
    task_id: str | None = None
    question_id: int | None = None


async def load_state() -> Attention:
    async with get_session() as s:
        row = await s.get(OwnerAttentionRow, 1)
    if row is None:
        return Attention()
    return Attention(row.state, row.wait_question_ref, row.wait_deadline, row.quiet_until)


async def save_state(state: Attention, now: datetime) -> None:
    async with get_session() as s:
        row = await s.get(OwnerAttentionRow, 1)
        if row is None:
            row = OwnerAttentionRow(id=1)
            s.add(row)
        row.state, row.wait_question_ref = state.state, state.wait_question_ref
        row.wait_deadline, row.quiet_until, row.updated_at = (
            state.wait_deadline, state.quiet_until, now)
        await s.commit()


async def defer(item: Deferred) -> None:
    async with get_session() as s:
        row = (await s.execute(select(OwnerDeferredRow).where(
            OwnerDeferredRow.dedup_key == item.dedup_key))).scalar_one_or_none()
        if row is None:
            row = OwnerDeferredRow(dedup_key=item.dedup_key)
            s.add(row)
        row.kind, row.text, row.created_at, row.sent_at = (
            item.kind, item.text, item.created_at, None)
        row.room, row.task_id, row.question_id = item.room, item.task_id, item.question_id
        await s.commit()


def _item(r: OwnerDeferredRow) -> Deferred:
    return Deferred(r.dedup_key, r.kind, r.text, r.created_at, r.room, r.task_id, r.question_id)


async def pending() -> list[Deferred]:
    async with get_session() as s:
        rows = (await s.execute(select(OwnerDeferredRow).where(
            OwnerDeferredRow.sent_at.is_(None)).order_by(OwnerDeferredRow.id))).scalars()
        return [_item(r) for r in rows]


async def mark_sent(keys: list[str], now: datetime) -> None:
    async with get_session() as s:
        rows = (await s.execute(select(OwnerDeferredRow).where(
            OwnerDeferredRow.dedup_key.in_(keys)))).scalars()
        for r in rows:
            r.sent_at = now
        await s.commit()


async def still_relevant(item: Deferred) -> bool:
    """Ответ получен или задача закрыта - пункт в сводку не идёт."""
    async with get_session() as s:
        if item.question_id is not None:
            q = await s.get(RoomTaskQuestionRow, item.question_id)
            if q is None or q.status != "open":
                return False
        if item.room and item.task_id:
            task = await s.get(RoomTaskRow, (item.room, item.task_id))
            if task is not None and task.status in CLOSED_TASK:
                return False
    return True
