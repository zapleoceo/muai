"""Правила аренды задачи над уже заблокированной строкой: захват/продление,
проверка живой аренды, снятие ожидания держателя."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from vera_shared.db.models_room import RoomTaskRow
from vera_shared.room.owner_block import owner_wait
from vera_shared.room.task_errors import StaleLease, TaskBusy, TaskNotFound
from vera_shared.timeutil import utc_naive_now
from vera_shared.voice_help.policy import SOURCE_CONFIRMED, SOURCE_UNCERTAIN_PREFIX

RELEASE_STATUSES = ("open", "done", "blocked")
HELD_STATUSES = ("in_progress", "blocked")


def _unconfirmed_voice(refs: list[dict[str, Any]] | None) -> bool:
    marks = {r.get("ref") for r in refs or []}
    return (SOURCE_CONFIRMED not in marks
            and any(isinstance(mark, str) and mark.startswith(SOURCE_UNCERTAIN_PREFIX)
                    for mark in marks))


def clear_holder_wait(row: RoomTaskRow) -> None:
    """Снимает только ожидание держателя; ожидание владельца может снять лишь владелец (S3)."""
    if owner_wait(row):
        return
    row.waiting_until, row.waiting_reason = None, None
    row.waiting_decision_owner = None


def apply_claim(row: RoomTaskRow, *, agent: str, now: datetime, lease_seconds: int,
                session: str | None, account: str | None) -> bool:
    """Захват или продление под блокировкой строки. True — продлена своя живая аренда.

    Продление меняет только поля аренды (S1). Новая аренда: open -> in_progress,
    blocked остаётся blocked, ожидание владельца не трогается (S2)."""
    live = bool(row.lease_holder and row.lease_until and row.lease_until > now)
    if live and row.lease_holder != agent:
        raise TaskBusy(f"task {row.task_id!r} is held by {row.lease_holder} "
                       f"until {row.lease_until.isoformat()}Z")
    if not live:
        row.fencing_token = (row.fencing_token or 0) + 1
        row.lease_holder = agent
        row.holder_session, row.holder_account = session, account
        clear_holder_wait(row)
        row.pending_handoff_to = None
        if row.status == "open":
            row.status = "in_progress"
    else:
        row.holder_session = session or row.holder_session
        row.holder_account = account or row.holder_account
    row.lease_until = now + timedelta(seconds=lease_seconds)
    row.updated_at = now
    return live


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
