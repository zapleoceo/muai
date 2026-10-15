"""Штатная отмена задачи без захвата: держатель аренды или создатель свободной задачи."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskQuestionRow, RoomTaskRow
from vera_shared.room.task_events import record_event
from vera_shared.room.task_view import task_dict
from vera_shared.room.tasks import StaleLease, TaskBusy, TaskNotFound, _locked
from vera_shared.timeutil import utc_naive_now
from vera_shared.voice_help.attest_state import attestation_for

FINAL_STATUSES = ("done", "cancelled")


def _authorize(row: RoomTaskRow, agent: str, fencing_token: int, now: Any) -> None:
    """Под блокировкой строки. Держатель — как в room_task_update; создатель — только без живой аренды."""
    live = bool(row.lease_holder and row.lease_until and row.lease_until > now)
    if row.fencing_token != fencing_token:
        raise StaleLease(f"stale fencing_token {fencing_token} for {row.task_id!r} "
                         f"(current {row.fencing_token})")
    if live:
        if row.lease_holder != agent:
            raise StaleLease(f"task {row.task_id!r} is held by another agent")
        return
    if row.created_by != agent:
        raise StaleLease(f"only the lease holder or the creator may cancel {row.task_id!r}")


async def cancel(*, room: str, task_id: str, agent: str, fencing_token: int,
                 reason: str) -> dict[str, Any]:
    reason = reason.strip()
    if not reason:
        raise ValueError("cancel reason must not be empty")
    async with get_session() as s:
        row = await _locked(s, room, task_id)
        if row is None:
            raise TaskNotFound(room, task_id)
        _authorize(row, agent, fencing_token, utc_naive_now())
        if row.status in FINAL_STATUSES:
            raise TaskBusy(f"task {task_id!r} is already {row.status}")
        if await attestation_for(s, room, task_id):
            raise TaskBusy("attested task cannot be cancelled by generic cancel")
        questions = (await s.execute(select(RoomTaskQuestionRow).where(
            RoomTaskQuestionRow.room == room,
            RoomTaskQuestionRow.task_id == task_id,
            RoomTaskQuestionRow.status.in_(("open", "answered")),
        ).order_by(RoomTaskQuestionRow.qid).with_for_update())).scalars().all()
        withdrawn_qids = [q.qid for q in questions]
        for question in questions:
            question.status = "withdrawn"
        prior = row.status
        was_holder = row.lease_holder == agent
        row.status = "cancelled"
        row.note = reason
        row.lease_holder = None
        row.lease_until = None
        row.pending_handoff_to = None
        row.updated_at = utc_naive_now()
        await record_event(s, room=room, task_id=task_id, kind="cancelled", agent=agent,
                           session=row.holder_session if was_holder else None,
                           account=row.holder_account if was_holder else None,
                           fencing_token=fencing_token, text=reason,
                           data={"status": "cancelled", "prior_status": prior,
                                 "reason": reason, "withdrawn_qids": withdrawn_qids})
        await s.refresh(row)
        return task_dict(row)
