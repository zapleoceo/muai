"""Очередь задач комнаты: список с вниманием и атомарный подбор следующей задачи."""
from __future__ import annotations

from typing import Any, Literal

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskEventRow, RoomTaskQuestionRow, RoomTaskRow
from vera_shared.room.task_attention import tasks_with_attention
from vera_shared.room.task_events import record_event
from vera_shared.room.task_view import task_dict
from vera_shared.room.tasks import apply_claim
from vera_shared.timeutil import utc_naive_now

Queue = Literal["open", "unclaimed"]
CANDIDATES = 50


def _no_live_lease(now: Any) -> Any:
    return or_(RoomTaskRow.lease_holder.is_(None), RoomTaskRow.lease_until.is_(None),
               RoomTaskRow.lease_until <= now)


def _open_question() -> Any:
    return exists().where(RoomTaskQuestionRow.room == RoomTaskRow.room,
                          RoomTaskQuestionRow.task_id == RoomTaskRow.task_id,
                          RoomTaskQuestionRow.status == "open")


async def list_tasks(*, room: str, status: str | None, limit: int,
                     queue: Queue | None = None) -> list[dict[str, Any]]:
    """queue=open — все незавершённые; queue=unclaimed — незавершённые без живой аренды."""
    now = utc_naive_now()
    q = select(RoomTaskRow).where(RoomTaskRow.room == room)
    if status is not None:
        q = q.where(RoomTaskRow.status == status)
    if queue is not None:
        q = q.where(RoomTaskRow.status != "done")
    if queue == "unclaimed":
        q = q.where(_no_live_lease(now))
    async with get_session() as s:
        rows = (await s.execute(q.order_by(RoomTaskRow.updated_at.desc()).limit(limit))
                ).scalars().all()
        return await tasks_with_attention(s, list(rows), now)


async def held_by(*, room: str, agent: str, limit: int = 50) -> list[dict[str, Any]]:
    """Незавершённые задачи, где agent записан держателем (в т.ч. с истёкшей арендой)."""
    q = select(RoomTaskRow).where(RoomTaskRow.room == room, RoomTaskRow.lease_holder == agent,
                                  RoomTaskRow.status != "done").limit(limit)
    async with get_session() as s:
        rows = (await s.execute(q)).scalars().all()
        return await tasks_with_attention(s, list(rows), utc_naive_now())


async def _is_paused(s: AsyncSession, row: RoomTaskRow) -> bool:
    last = (await s.execute(
        select(RoomTaskEventRow.kind)
        .where(RoomTaskEventRow.room == row.room, RoomTaskEventRow.task_id == row.task_id,
               RoomTaskEventRow.kind.in_(("paused", "resumed")))
        .order_by(RoomTaskEventRow.id.desc()).limit(1))).scalar_one_or_none()
    return last == "paused"


async def _deps_done(s: AsyncSession, row: RoomTaskRow) -> bool:
    deps = set(row.depends_on or [])
    if not deps:
        return True
    done = (await s.execute(select(RoomTaskRow.task_id).where(
        RoomTaskRow.room == row.room, RoomTaskRow.task_id.in_(deps),
        RoomTaskRow.status == "done"))).scalars().all()
    return len(set(done)) == len(deps)


async def _still_eligible(s: AsyncSession, row: RoomTaskRow, now: Any) -> bool:
    if (row.status != "open" or not row.auto_pickup
            or (row.lease_holder and row.lease_until and row.lease_until > now)):
        return False
    has_question = (await s.execute(select(RoomTaskQuestionRow.qid).where(
        RoomTaskQuestionRow.room == row.room, RoomTaskQuestionRow.task_id == row.task_id,
        RoomTaskQuestionRow.status == "open").limit(1))).first()
    return has_question is None and await _deps_done(s, row) and not await _is_paused(s, row)


async def next_task(*, room: str, agent: str, project: str | None, lease_seconds: int,
                    session: str | None, account: str | None) -> dict[str, Any] | None:
    """Берёт одну подходящую задачу. Каждый кандидат блокируется FOR UPDATE SKIP LOCKED
    и перепроверяется под блокировкой: параллельный подбор не отдаст одну задачу двоим."""
    q = (select(RoomTaskRow.task_id).where(
        RoomTaskRow.room == room, RoomTaskRow.status == "open",
        RoomTaskRow.auto_pickup.is_(True), _no_live_lease(utc_naive_now()),
        ~_open_question())
        .order_by(RoomTaskRow.priority, RoomTaskRow.created_at).limit(CANDIDATES))
    if project is not None:
        q = q.where(RoomTaskRow.project == project)
    async with get_session() as s:
        candidates = (await s.execute(q)).scalars().all()
    for task_id in candidates:
        async with get_session() as s:
            row = (await s.execute(
                select(RoomTaskRow).where(RoomTaskRow.room == room,
                                          RoomTaskRow.task_id == task_id)
                .with_for_update(skip_locked=True))).scalar_one_or_none()
            now = utc_naive_now()  # после блокировки: ожидание могло занять время
            if row is None or not await _still_eligible(s, row, now):
                continue
            apply_claim(row, agent=agent, now=now, lease_seconds=lease_seconds,
                        session=session, account=account)
            await record_event(s, room=room, task_id=task_id, kind="claimed", agent=agent,
                               session=session, account=account,
                               fencing_token=row.fencing_token, data={"via": "next"})
            await s.refresh(row)
            return task_dict(row)
    return None
