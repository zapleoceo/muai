"""Передача задачи другому агенту: предложение держателя и подтверждение получателя.

Аренда остаётся у отправителя до приёма. Приём переносит её получателю с
fencing_token+1, так что старый токен отправителя сразу становится устаревшим.
Отказ и отмена — тоже события `handoff_offer` с `data.action` = decline/cancel
(отдельного вида в CHECK нет, миграция не нужна).
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskEventRow
from vera_shared.room.messages import add_message
from vera_shared.room.task_events import record_event
from vera_shared.room.tasks import _locked, _require_lease, task_dict
from vera_shared.timeutil import utc_naive_now

DEFAULT_HANDOFF_LEASE_S = 900


class HandoffError(RuntimeError):
    """Передача невозможна: нет предложения, оно не тебе или уже есть другое."""


def _pending(row: Any, task_id: str) -> str:
    if row is None or not row.pending_handoff_to:
        raise HandoffError(f"no pending handoff on {task_id!r}")
    return row.pending_handoff_to


async def offer(*, room: str, task_id: str, agent: str, fencing_token: int, to_agent: str,
                note: str | None = None) -> dict[str, Any]:
    if to_agent == agent:
        raise ValueError("cannot hand off a task to yourself")
    async with get_session() as s:
        row = _require_lease(await _locked(s, room, task_id), room, task_id, agent,
                             fencing_token)
        if row.pending_handoff_to:
            raise HandoffError(f"task {task_id!r} already offered to "
                               f"{row.pending_handoff_to}; cancel first")
        row.pending_handoff_to = to_agent
        row.updated_at = utc_naive_now()
        await record_event(s, room=room, task_id=task_id, kind="handoff_offer", agent=agent,
                           session=row.holder_session, account=row.holder_account,
                           fencing_token=fencing_token, text=note,
                           data={"action": "offer", "to": to_agent})
        body = f"Передаю задачу {task_id}" + (f": {note}" if note else "")
        await add_message(s, room=room, message_id=f"handoff-{uuid.uuid4().hex[:16]}",
                          from_agent=agent, to_agent=to_agent, task_id=task_id,
                          body=body, status="request")
        await s.refresh(row)
        return task_dict(row)


async def accept(*, room: str, task_id: str, agent: str, session: str | None = None,
                 account: str | None = None,
                 lease_seconds: int = DEFAULT_HANDOFF_LEASE_S) -> dict[str, Any]:
    async with get_session() as s:
        row = await _locked(s, room, task_id)
        if _pending(row, task_id) != agent:
            raise HandoffError(f"handoff of {task_id!r} is not offered to {agent}")
        if row.status in ("done", "cancelled"):
            raise HandoffError(f"task {task_id!r} is {row.status}; handoff cannot be accepted")
        last_state = (await s.execute(select(RoomTaskEventRow.kind).where(
            RoomTaskEventRow.room == room, RoomTaskEventRow.task_id == task_id,
            RoomTaskEventRow.kind.in_(("paused", "resumed")))
            .order_by(RoomTaskEventRow.id.desc()).limit(1))).scalar_one_or_none()
        if last_state == "paused":
            raise HandoffError(f"task {task_id!r} is paused; resume it before accepting")
        now = utc_naive_now()
        sender = row.lease_holder
        row.lease_holder, row.lease_until = agent, now + timedelta(seconds=lease_seconds)
        row.fencing_token = (row.fencing_token or 0) + 1
        row.holder_session, row.holder_account = session, account
        row.pending_handoff_to = None
        row.status = "in_progress"
        row.waiting_until, row.waiting_reason = None, None
        row.updated_at = now
        await record_event(s, room=room, task_id=task_id, kind="handoff_accept", agent=agent,
                           session=session, account=account, fencing_token=row.fencing_token,
                           data={"from": sender})
        await s.refresh(row)
        return task_dict(row)


async def _clear(room: str, task_id: str, agent: str, action: str,
                 fencing_token: int | None, reason: str | None) -> dict[str, Any]:
    async with get_session() as s:
        row = await _locked(s, room, task_id)
        to = _pending(row, task_id)
        if action == "decline" and to != agent:
            raise HandoffError(f"handoff of {task_id!r} is not offered to {agent}")
        if action == "cancel":
            _require_lease(row, room, task_id, agent, fencing_token or 0)
        row.pending_handoff_to = None
        row.updated_at = utc_naive_now()
        await record_event(s, room=room, task_id=task_id, kind="handoff_offer", agent=agent,
                           fencing_token=row.fencing_token, text=reason,
                           data={"action": action, "to": to})
        await s.refresh(row)
        return task_dict(row)


async def decline(*, room: str, task_id: str, agent: str,
                  reason: str | None = None) -> dict[str, Any]:
    return await _clear(room, task_id, agent, "decline", None, reason)


async def cancel(*, room: str, task_id: str, agent: str, fencing_token: int,
                 reason: str | None = None) -> dict[str, Any]:
    return await _clear(room, task_id, agent, "cancel", fencing_token, reason)
