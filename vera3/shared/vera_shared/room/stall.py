"""Поиск зависших задач для пульта владельца: чтение таблиц комнаты, решение — в `stall_rules`."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskEventRow, RoomTaskRow
from vera_shared.room.owner_block import owner_block
from vera_shared.room.owner_task_actions import SNOOZE_ACTION
from vera_shared.room.stall_rules import ACTIVE_STATUSES, StallFacts, is_stalled
from vera_shared.timeutil import parse_iso_naive, utc_naive_now
from vera_shared.voice_help.attest_state import attestation_for


@dataclass(frozen=True)
class Stalled:
    room: str
    task_id: str
    title: str | None
    project: str | None
    status: str
    note: str | None
    next_action: str | None
    idle: timedelta


def last_activity(row: RoomTaskRow) -> datetime:
    marks = [t for t in (row.updated_at, row.last_progress_at, row.created_at) if t is not None]
    return max(marks)


async def _snooze_until(s: AsyncSession, room: str, task_id: str) -> datetime | None:
    data = (await s.execute(select(RoomTaskEventRow.data).where(
        RoomTaskEventRow.room == room, RoomTaskEventRow.task_id == task_id,
        RoomTaskEventRow.kind == "watchdog_action").order_by(RoomTaskEventRow.id.desc())
    )).scalars().all()
    for item in data:
        if item and item.get("action") == SNOOZE_ACTION:
            try:
                return parse_iso_naive(str(item.get("until")))
            except ValueError:
                return None  # битая отметка не прячет задачу с пульта
    return None


async def facts_of(s: AsyncSession, row: RoomTaskRow, now: datetime) -> StallFacts:
    return StallFacts(
        status=row.status,
        live_lease=bool(row.lease_holder and row.lease_until and row.lease_until > now),
        owner_blocked=(await owner_block(s, row)).active,
        recovery_hold=bool(row.recovery_hold),
        attested=await attestation_for(s, row.room, row.task_id) is not None,
        waiting_until=row.waiting_until,
        last_activity=last_activity(row),
        snooze_until=await _snooze_until(s, row.room, row.task_id))


def _view(row: RoomTaskRow, now: datetime) -> Stalled:
    return Stalled(room=row.room, task_id=row.task_id, title=row.title, project=row.project,
                   status=row.status, note=row.note, next_action=row.next_action,
                   idle=now - last_activity(row))


async def stalled_tasks(now: datetime | None = None) -> list[Stalled]:
    """Зависшие задачи, самые давние первыми."""
    now = now or utc_naive_now()
    async with get_session() as s:
        rows = (await s.execute(select(RoomTaskRow).where(
            RoomTaskRow.status.in_(ACTIVE_STATUSES)))).scalars().all()
        found = [_view(row, now) for row in rows if is_stalled(await facts_of(s, row, now), now)]
    return sorted(found, key=lambda t: (-t.idle, t.room, t.task_id))
