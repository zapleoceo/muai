"""SQL страницы `/tasks`: строки задач и журнал событий, только чтение."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from vera_shared.db.models_room import RoomTaskEventRow, RoomTaskRow

ROWS_LIMIT = 500
EVENTS_LIMIT = 200
GANTT_EVENTS_LIMIT = 20000


async def all_tasks(s: AsyncSession) -> list[RoomTaskRow]:
    q = select(RoomTaskRow).order_by(RoomTaskRow.updated_at.desc()).limit(ROWS_LIMIT)
    return list((await s.execute(q)).scalars().all())


async def one_task(s: AsyncSession, room: str, task_id: str) -> RoomTaskRow | None:
    return await s.get(RoomTaskRow, (room, task_id))


async def task_events(s: AsyncSession, room: str, task_id: str) -> list[RoomTaskEventRow]:
    q = (select(RoomTaskEventRow)
         .where(RoomTaskEventRow.room == room, RoomTaskEventRow.task_id == task_id)
         .order_by(RoomTaskEventRow.id).limit(EVENTS_LIMIT))
    return list((await s.execute(q)).scalars().all())


async def events_of_tasks(s: AsyncSession, keys: list[tuple[str, str]]) -> list[RoomTaskEventRow]:
    """События набора задач по id; пары (room, task_id) сверяются уже в Python."""
    if not keys:
        return []
    q = (select(RoomTaskEventRow)
         .where(RoomTaskEventRow.task_id.in_({k[1] for k in keys}))
         .order_by(RoomTaskEventRow.id).limit(GANTT_EVENTS_LIMIT))
    wanted = set(keys)
    return [e for e in (await s.execute(q)).scalars().all() if (e.room, e.task_id) in wanted]
