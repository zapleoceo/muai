"""Задачи комнаты: аренда (lease) + fencing-токен, чтобы двое не правили одно и то же.

Захват свободной или просроченной задачи увеличивает fencing_token; продление
своей живой аренды его не меняет. Любая правка/освобождение требует текущий
токен и живую аренду: агент, у которого аренда истекла и задачу перехватили,
получает отказ, даже если «проснулся» и продолжил с того же места.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskRow
from vera_shared.timeutil import utc_naive_now

RELEASE_STATUSES = ("open", "done", "blocked")
HELD_STATUSES = ("in_progress", "blocked")


class TaskNotFound(LookupError):
    def __init__(self, room: str, task_id: str) -> None:
        super().__init__(f"task {task_id!r} not found in room {room!r}")


class TaskBusy(RuntimeError):
    """Задачу держит другой агент с живой арендой."""


class StaleLease(RuntimeError):
    """Аренда не твоя, истекла или fencing_token устарел."""


def task_dict(r: RoomTaskRow) -> dict[str, Any]:
    now = utc_naive_now()
    live = bool(r.lease_holder and r.lease_until and r.lease_until > now)
    return {"task_id": r.task_id, "room": r.room, "title": r.title, "status": r.status,
            "created_by": r.created_by, "lease_holder": r.lease_holder if live else None,
            "lease_until": r.lease_until.isoformat() if live and r.lease_until else None,
            "fencing_token": r.fencing_token, "paths": list(r.paths or []), "note": r.note,
            "updated_at": r.updated_at.isoformat()}


async def _locked(s: Any, room: str, task_id: str) -> RoomTaskRow | None:
    return await s.get(RoomTaskRow, (room, task_id), with_for_update=True)


async def open_task(*, room: str, task_id: str, agent: str, title: str | None,
                    paths: list[str] | None) -> tuple[dict[str, Any], bool]:
    """(задача, created). Уже существующая задача возвращается как есть."""
    try:
        async with get_session() as s:
            row = await _locked(s, room, task_id)
            if row is not None:
                return task_dict(row), False
            row = RoomTaskRow(room=room, task_id=task_id, title=title, created_by=agent,
                              status="open", fencing_token=0, paths=paths or [])
            s.add(row)
            await s.flush()
            await s.refresh(row)
            return task_dict(row), True
    except IntegrityError:
        async with get_session() as s:
            row = await s.get(RoomTaskRow, (room, task_id))
            if row is None:
                raise
            return task_dict(row), False


async def claim(*, room: str, task_id: str, agent: str, lease_seconds: int,
                title: str | None = None, paths: list[str] | None = None,
                ) -> dict[str, Any]:
    now = utc_naive_now()
    try:
        async with get_session() as s:
            row = await _locked(s, room, task_id)
            if row is None:
                row = RoomTaskRow(room=room, task_id=task_id, title=title, created_by=agent,
                                  status="open", fencing_token=0, paths=paths or [])
                s.add(row)
            elif row.status == "done":
                raise TaskBusy(f"task {task_id!r} is done; open a new task_id")
            live = bool(row.lease_holder and row.lease_until and row.lease_until > now)
            if live and row.lease_holder != agent:
                raise TaskBusy(f"task {task_id!r} is held by {row.lease_holder} "
                               f"until {row.lease_until.isoformat()}Z")
            if not live:
                row.fencing_token = (row.fencing_token or 0) + 1
                row.lease_holder = agent
            row.lease_until = now + timedelta(seconds=lease_seconds)
            row.status = "in_progress"
            if title is not None:
                row.title = title
            if paths is not None:
                row.paths = paths
            row.updated_at = now
            await s.flush()
            await s.refresh(row)
            return task_dict(row)
    except IntegrityError:
        raise TaskBusy(f"task {task_id!r} was created concurrently; retry the claim"
                       ) from None


def _require_lease(row: RoomTaskRow | None, room: str, task_id: str, agent: str,
                   fencing_token: int) -> RoomTaskRow:
    if row is None:
        raise TaskNotFound(room, task_id)
    now = utc_naive_now()
    if (row.lease_holder != agent or row.fencing_token != fencing_token
            or row.lease_until is None or row.lease_until <= now):
        raise StaleLease(f"no live lease on {task_id!r} for {agent} with fencing_token "
                         f"{fencing_token} (current {row.fencing_token})")
    return row


async def update(*, room: str, task_id: str, agent: str, fencing_token: int,
                 status: str | None = None, note: str | None = None,
                 extend_seconds: int | None = None) -> dict[str, Any]:
    if status is not None and status not in HELD_STATUSES:
        raise ValueError(f"status while holding must be one of {', '.join(HELD_STATUSES)}")
    async with get_session() as s:
        row = _require_lease(await _locked(s, room, task_id), room, task_id, agent,
                             fencing_token)
        now = utc_naive_now()
        if status is not None:
            row.status = status
        if note is not None:
            row.note = note
        if extend_seconds is not None:
            row.lease_until = now + timedelta(seconds=extend_seconds)
        row.updated_at = now
        await s.flush()
        await s.refresh(row)
        return task_dict(row)


async def release(*, room: str, task_id: str, agent: str, fencing_token: int,
                  status: str, note: str | None = None) -> dict[str, Any]:
    if status not in RELEASE_STATUSES:
        raise ValueError(f"release status must be one of {', '.join(RELEASE_STATUSES)}")
    async with get_session() as s:
        row = _require_lease(await _locked(s, room, task_id), room, task_id, agent,
                             fencing_token)
        row.status = status
        row.lease_holder = None
        row.lease_until = None
        if note is not None:
            row.note = note
        row.updated_at = utc_naive_now()
        await s.flush()
        await s.refresh(row)
        return task_dict(row)


async def list_tasks(*, room: str, status: str | None, limit: int) -> list[dict[str, Any]]:
    q = select(RoomTaskRow).where(RoomTaskRow.room == room)
    if status is not None:
        q = q.where(RoomTaskRow.status == status)
    async with get_session() as s:
        rows = (await s.execute(q.order_by(RoomTaskRow.updated_at.desc()).limit(limit))
                ).scalars().all()
    return [task_dict(r) for r in rows]
