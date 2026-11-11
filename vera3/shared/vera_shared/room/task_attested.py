"""Закрытие задачи с делегированной аттестацией по квитанции уже отправленного ответа."""
from __future__ import annotations

from typing import Any

from vera_shared.db.engine import get_session
from vera_shared.room.task_errors import TaskBusy, TaskNotFound
from vera_shared.room.task_events import record_event
from vera_shared.room.task_lease import _require_lease
from vera_shared.room.task_store import (
    _locked,
    last_done_event,
    lock_receipt,
    receipt_already_used,
)
from vera_shared.room.task_view import task_dict
from vera_shared.timeutil import utc_naive_now
from vera_shared.voice_help.attest_state import attestation_for


async def complete_attested(*, room: str, task_id: str, agent: str, fencing_token: int,
                            reply_text: str, receipt_id: str) -> dict[str, Any]:
    """Record dot's assertion of a pre-existing reply; this never sends a message."""
    if agent != "dot" or not receipt_id or len(receipt_id) > 128:
        raise TaskBusy("only dot may record a bounded reply receipt")
    async with get_session() as s:
        row = await _locked(s, room, task_id)
        if row is None:
            raise TaskNotFound(room, task_id)
        attestation = await attestation_for(s, room, task_id)
        if attestation is None:
            raise TaskBusy("task has no delegated attestation")
        allowed = (attestation.data or {}).get("allowed_reply")
        if not allowed or reply_text != allowed:
            raise TaskBusy("reply text differs from attested literal word")
        if row.status == "done":
            prior = await last_done_event(s, room, task_id)
            data = prior.data or {} if prior else {}
            if data.get("receipt_id") == receipt_id and data.get("reply_text") == reply_text:
                return {"task": task_dict(row), "replayed": True}
            raise TaskBusy("task already completed with another receipt")
        _require_lease(row, room, task_id, agent, fencing_token)
        await lock_receipt(s, receipt_id)
        if await receipt_already_used(s, receipt_id):
            raise TaskBusy("receipt is already bound to another completed task")
        row.status = "done"
        row.lease_holder = None
        row.lease_until = None
        row.waiting_until, row.waiting_reason = None, None
        row.waiting_decision_owner = None
        row.pending_handoff_to = None
        row.note = f"Existing reply receipt: {receipt_id}"
        row.updated_at = utc_naive_now()
        await record_event(s, room=room, task_id=task_id, kind="done", agent=agent,
                           fencing_token=fencing_token, text=row.note,
                           data={"status": "done", "receipt_id": receipt_id,
                                 "reply_text": reply_text, "attestation_event_id": attestation.id})
        await s.refresh(row)
        return {"task": task_dict(row), "replayed": False}
