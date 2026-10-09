"""Задачи комнаты: аренда (lease) + fencing-токен, чтобы двое не правили одно и то же.

Захват свободной или просроченной задачи увеличивает fencing_token; продление
своей живой аренды его не меняет. Любая правка/освобождение требует текущий
токен и живую аренду: агент, у которого аренда истекла и задачу перехватили,
получает отказ, даже если «проснулся» и продолжил с того же места.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError

from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskRow
from vera_shared.room.task_events import record_event
from vera_shared.room.task_fields import (
    clean_responsible,
    validate_depends_on,
    validate_open_fields,
    validate_project,
)
from vera_shared.room.task_refs import validate_refs
from vera_shared.room.task_view import task_dict
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


async def _locked(s: Any, room: str, task_id: str) -> RoomTaskRow | None:
    return await s.get(RoomTaskRow, (room, task_id), with_for_update=True)


async def open_task(*, room: str, task_id: str, agent: str, title: str | None,
                    paths: list[str] | None, project: str | None = None,
                    priority: int | None = None, auto_pickup: bool | None = None,
                    depends_on: list[str] | None = None, next_action: str | None = None,
                    refs: list[dict[str, Any]] | None = None,
                    responsible: str | None = None) -> tuple[dict[str, Any], bool]:
    """(задача, created). Существующая возвращается как есть, поля очереди не меняются."""
    extra = validate_open_fields(task_id, project=project, priority=priority,
                                 auto_pickup=auto_pickup, depends_on=depends_on,
                                 next_action=next_action, refs=refs,
                                 responsible=responsible)
    try:
        async with get_session() as s:
            row = await _locked(s, room, task_id)
            if row is not None:
                return task_dict(row), False
            row = RoomTaskRow(room=room, task_id=task_id, title=title, created_by=agent,
                              status="open", fencing_token=0, paths=paths or [], **extra)
            s.add(row)
            await record_event(s, room=room, task_id=task_id, kind="created", agent=agent,
                               text=title)
            await s.refresh(row)
            return task_dict(row), True
    except IntegrityError:
        async with get_session() as s:
            row = await s.get(RoomTaskRow, (room, task_id))
            if row is None:
                raise
            return task_dict(row), False


def apply_claim(row: RoomTaskRow, *, agent: str, now: datetime, lease_seconds: int,
                session: str | None, account: str | None) -> bool:
    """Захват или продление под блокировкой строки. True — продлена своя живая аренда."""
    live = bool(row.lease_holder and row.lease_until and row.lease_until > now)
    if live and row.lease_holder != agent:
        raise TaskBusy(f"task {row.task_id!r} is held by {row.lease_holder} "
                       f"until {row.lease_until.isoformat()}Z")
    if not live:
        row.fencing_token = (row.fencing_token or 0) + 1
        row.lease_holder = agent
        row.holder_session, row.holder_account = session, account
        row.waiting_until, row.waiting_reason = None, None
    else:
        row.holder_session = session or row.holder_session
        row.holder_account = account or row.holder_account
    row.lease_until = now + timedelta(seconds=lease_seconds)
    row.status = "in_progress"
    row.updated_at = now
    return live


async def claim(*, room: str, task_id: str, agent: str, lease_seconds: int,
                title: str | None = None, paths: list[str] | None = None,
                session: str | None = None, account: str | None = None,
                ) -> dict[str, Any]:
    try:
        async with get_session() as s:
            row = await _locked(s, room, task_id)
            # время — после блокировки строки: ожидание лока могло длиться дольше аренды
            now = utc_naive_now()
            if row is None:
                row = RoomTaskRow(room=room, task_id=task_id, title=title, created_by=agent,
                                  status="open", fencing_token=0, paths=paths or [])
                s.add(row)
                await record_event(s, room=room, task_id=task_id, kind="created",
                                   agent=agent, text=title)
            elif row.status == "done":
                raise TaskBusy(f"task {task_id!r} is done; open a new task_id")
            live = apply_claim(row, agent=agent, now=now, lease_seconds=lease_seconds,
                               session=session, account=account)
            if title is not None:
                row.title = title
            if paths is not None:
                row.paths = paths
            await record_event(s, room=room, task_id=task_id,
                               kind="heartbeat" if live else "claimed", agent=agent,
                               session=session, account=account,
                               fencing_token=row.fencing_token)
            await s.refresh(row)
            return task_dict(row)
    except IntegrityError:
        raise TaskBusy(f"task {task_id!r} was created concurrently; retry the claim"
                       ) from None


def _require_lease(row: RoomTaskRow | None, room: str, task_id: str, agent: str,
                   fencing_token: int) -> RoomTaskRow:
    if row is None:
        raise TaskNotFound(room, task_id)
    now = utc_naive_now()  # зовётся уже под блокировкой строки
    if (row.lease_holder != agent or row.fencing_token != fencing_token
            or row.lease_until is None or row.lease_until <= now):
        raise StaleLease(f"no live lease on {task_id!r} for {agent} with fencing_token "
                         f"{fencing_token} (current {row.fencing_token})")
    return row


async def update(*, room: str, task_id: str, agent: str, fencing_token: int,
                 status: str | None = None, note: str | None = None,
                 extend_seconds: int | None = None, next_action: str | None = None,
                 priority: int | None = None, refs: list[dict[str, Any]] | None = None,
                 project: str | None = None, depends_on: list[str] | None = None,
                 auto_pickup: bool | None = None,
                 responsible: str | None = None) -> dict[str, Any]:
    if priority is not None and not 0 <= priority <= 3:
        raise ValueError("priority must be 0..3 (0 most urgent, default 2)")
    clean_refs = validate_refs(refs) if refs is not None else None
    clean_deps = (validate_depends_on(task_id, depends_on)
                  if depends_on is not None else None)
    clean_project = validate_project(project) if project is not None else None
    clean_resp = clean_responsible(responsible)
    if status is not None and status not in HELD_STATUSES:
        raise ValueError(f"status while holding must be one of {', '.join(HELD_STATUSES)}")
    async with get_session() as s:
        row = _require_lease(await _locked(s, room, task_id), room, task_id, agent,
                             fencing_token)
        now = utc_naive_now()
        prev_status = row.status
        if status is not None:
            row.status = status
        if note is not None:
            row.note = note
        if extend_seconds is not None:
            row.lease_until = now + timedelta(seconds=extend_seconds)
        if next_action is not None:
            row.next_action = next_action
        if priority is not None:
            row.priority = priority
        if clean_refs is not None:
            row.refs = clean_refs
        if clean_project is not None:
            row.project = clean_project
        if clean_deps is not None:
            row.depends_on = clean_deps
        if auto_pickup is not None:
            row.auto_pickup = auto_pickup
        if clean_resp is not None:
            row.responsible = clean_resp
        row.updated_at = now
        if status is not None or note is not None:
            # прогресс — содержательная правка; одно продление аренды (heartbeat) его не двигает
            row.last_progress_at = now
            row.last_progress_text = note if note is not None else f"status: {status}"
            row.waiting_until, row.waiting_reason = None, None
            kind = "progress"
            # переход блокировки пишется раньше progress: Гантт не рисует вспышку работы
            flip = ("blocked" if status == "blocked" and prev_status != "blocked" else
                    "unblocked" if prev_status == "blocked" and status == "in_progress"
                    else None)
            if flip:
                await record_event(s, room=room, task_id=task_id, kind=flip, agent=agent,
                                   session=row.holder_session, account=row.holder_account,
                                   fencing_token=fencing_token, data={"status": status})
        else:
            kind = "heartbeat"
        await record_event(s, room=room, task_id=task_id, kind=kind, agent=agent,
                           session=row.holder_session, account=row.holder_account,
                           fencing_token=fencing_token, text=note,
                           data={"status": status} if status is not None else None)
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
        row.waiting_until, row.waiting_reason = None, None
        if note is not None:
            row.note = note
        row.updated_at = utc_naive_now()
        await record_event(s, room=room, task_id=task_id,
                           kind="done" if status == "done" else "released", agent=agent,
                           session=row.holder_session, account=row.holder_account,
                           fencing_token=fencing_token, text=note, data={"status": status})
        await s.refresh(row)
        return task_dict(row)
