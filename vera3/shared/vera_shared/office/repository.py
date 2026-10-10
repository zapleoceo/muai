"""SQL office intake. origin и result_recipient пишутся один раз: пути обновления нет."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.models_office import OfficeAnswerKeyRow, RoomTaskOriginRow
from vera_shared.db.models_room import RoomTaskEventRow, RoomTaskRow


async def origin_by_client(s: AsyncSession, principal: str, client_task_id: str,
                           ) -> RoomTaskOriginRow | None:
    return (await s.execute(select(RoomTaskOriginRow).where(
        RoomTaskOriginRow.creator_principal == principal,
        RoomTaskOriginRow.client_task_id == client_task_id))).scalar_one_or_none()


async def origin_of(s: AsyncSession, task_id: str) -> RoomTaskOriginRow | None:
    return (await s.execute(select(RoomTaskOriginRow).where(
        RoomTaskOriginRow.task_id == task_id))).scalar_one_or_none()


async def insert_origin(s: AsyncSession, **fields: Any) -> RoomTaskOriginRow:
    row = RoomTaskOriginRow(**fields)
    s.add(row)
    await s.flush()
    return row


async def drop_origin(s: AsyncSession, room: str, task_id: str) -> None:
    row = await s.get(RoomTaskOriginRow, (room, task_id))
    if row is not None:
        await s.delete(row)


async def revision_of(s: AsyncSession, room: str, task_id: str) -> int:
    """Ревизия = id последней строки журнала задачи (тот же источник, что у MCP Events)."""
    return (await s.execute(select(func.max(RoomTaskEventRow.id)).where(
        RoomTaskEventRow.room == room, RoomTaskEventRow.task_id == task_id))).scalar() or 0


async def result_text_of(s: AsyncSession, room: str, task_id: str) -> str | None:
    return (await s.execute(select(RoomTaskEventRow.text).where(
        RoomTaskEventRow.room == room, RoomTaskEventRow.task_id == task_id,
        RoomTaskEventRow.kind.in_(("done", "review"))).order_by(
            RoomTaskEventRow.id.desc()).limit(1))).scalar_one_or_none()


async def answer_key(s: AsyncSession, task_id: str, key: str) -> OfficeAnswerKeyRow | None:
    return (await s.execute(select(OfficeAnswerKeyRow).where(
        OfficeAnswerKeyRow.task_id == task_id,
        OfficeAnswerKeyRow.idempotency_key == key))).scalar_one_or_none()


async def insert_answer_key(s: AsyncSession, **fields: Any) -> OfficeAnswerKeyRow:
    row = OfficeAnswerKeyRow(**fields)
    s.add(row)
    await s.flush()
    return row


async def drop_answer_key(s: AsyncSession, task_id: str, key: str) -> None:
    row = await answer_key(s, task_id, key)
    if row is not None:
        await s.delete(row)


async def set_key_revision(s: AsyncSession, task_id: str, key: str, revision: int) -> None:
    await s.execute(update(OfficeAnswerKeyRow).where(
        OfficeAnswerKeyRow.task_id == task_id,
        OfficeAnswerKeyRow.idempotency_key == key).values(revision_after=revision))


async def raise_delivered(s: AsyncSession, room: str, task_id: str, revision: int) -> int:
    """Монотонно: меньшая ревизия не откатывает отметку. → итоговое значение."""
    await s.execute(update(RoomTaskOriginRow).where(
        RoomTaskOriginRow.room == room, RoomTaskOriginRow.task_id == task_id,
        RoomTaskOriginRow.delivered_revision < revision).values(delivered_revision=revision))
    row = await s.get(RoomTaskOriginRow, (room, task_id), populate_existing=True)
    return row.delivered_revision if row else 0


async def own_tasks_page(s: AsyncSession, *, principal: str, project: str, room: str,
                         since: datetime | None, after: tuple[datetime, str] | None,
                         limit: int) -> list[tuple[RoomTaskOriginRow, RoomTaskRow]]:
    q = (select(RoomTaskOriginRow, RoomTaskRow)
         .join(RoomTaskRow, (RoomTaskRow.room == RoomTaskOriginRow.room)
               & (RoomTaskRow.task_id == RoomTaskOriginRow.task_id))
         .where(RoomTaskOriginRow.creator_principal == principal,
                RoomTaskOriginRow.room == room, RoomTaskRow.project == project))
    if since is not None:
        q = q.where(RoomTaskRow.updated_at >= since)
    if after is not None:
        at, tid = after
        q = q.where(or_(RoomTaskRow.updated_at > at,
                        (RoomTaskRow.updated_at == at) & (RoomTaskRow.task_id > tid)))
    q = q.order_by(RoomTaskRow.updated_at, RoomTaskRow.task_id).limit(limit)
    return [(r[0], r[1]) for r in (await s.execute(q)).all()]
