"""Lease-free correction of task coordination fields by the existing dot actor."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskEventRow, RoomTaskQuestionRow
from vera_shared.room.task_events import record_event
from vera_shared.room.task_fields import clean_responsible
from vera_shared.room.task_view import task_dict
from vera_shared.room.tasks import TaskBusy, TaskNotFound, _locked
from vera_shared.timeutil import utc_naive_now
from vera_shared.voice_help.attest_state import attestation_for


async def correct_metadata(*, room: str, task_id: str, agent: str,
                           expected_updated_at: str, reason: str,
                           responsible: str | None = None,
                           next_action: str | None = None) -> dict[str, Any]:
    if agent != "dot":
        raise TaskBusy("metadata correction is reserved for the existing dot actor")
    if responsible is None and next_action is None:
        raise ValueError("pass responsible or next_action")
    if next_action is not None and len(next_action) > 2000:
        raise ValueError("next_action must be at most 2000 chars")
    if not reason.strip() or len(reason) > 500:
        raise ValueError("reason must be 1..500 chars")
    clean_resp = clean_responsible(responsible) if responsible is not None else None
    async with get_session() as s:
        row = await _locked(s, room, task_id)
        if row is None:
            raise TaskNotFound(room, task_id)
        now = utc_naive_now()
        if (row.status not in ("open", "blocked") or row.owner is not None
                or row.pending_handoff_to is not None
                or (row.lease_holder and row.lease_until and row.lease_until > now)):
            raise TaskBusy("task is not eligible for metadata-only correction")
        question = (await s.execute(select(RoomTaskQuestionRow.qid).where(
            RoomTaskQuestionRow.room == room, RoomTaskQuestionRow.task_id == task_id,
            RoomTaskQuestionRow.status == "open").limit(1))).first()
        last_state = (await s.execute(select(RoomTaskEventRow.kind).where(
            RoomTaskEventRow.room == room, RoomTaskEventRow.task_id == task_id,
            RoomTaskEventRow.kind.in_(("paused", "review", "resumed")))
            .order_by(RoomTaskEventRow.id.desc()).limit(1))).scalar_one_or_none()
        if (question or last_state in ("paused", "review")
                or await attestation_for(s, room, task_id)):
            raise TaskBusy("task is held for owner review or attestation")
        before = {"responsible": row.responsible, "next_action": row.next_action}
        after = {"responsible": clean_resp if responsible is not None else row.responsible,
                 "next_action": next_action if next_action is not None else row.next_action}
        if before == after:
            return {"task": task_dict(row), "changed": False}
        if row.updated_at.isoformat() != expected_updated_at:
            raise TaskBusy("task changed since metadata was read")
        row.responsible, row.next_action = after["responsible"], after["next_action"]
        row.updated_at = now
        await record_event(s, room=room, task_id=task_id, kind="metadata_updated",
                           agent=agent, text=reason.strip(),
                           data={"before": before, "after": after})
        await s.refresh(row)
        return {"task": task_dict(row), "changed": True}
