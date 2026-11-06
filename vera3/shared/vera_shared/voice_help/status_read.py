"""Whitelisted diagnostics for the existing Room task-history permission."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskRow
from vera_shared.db.models_voice import VoiceCommandRow
from vera_shared.room.messages import has_reply
from vera_shared.voice_commands import GUARD_VERSION
from vera_shared.voice_help.fact_route import external_facts
from vera_shared.voice_help.policy import help_room, source_uncertain
from vera_shared.voice_help.result_state import executor_result, parse_since
from vera_shared.voice_help.tracking import holder_of


async def voice_status_for_task(room: str, task_id: str) -> dict[str, Any] | None:
    """No instruction, source window, event content, Telegram owner ID, or queue listing."""
    if room != help_room():
        return None
    async with get_session() as s:
        task = await s.get(RoomTaskRow, (room, task_id))
        if task is None or task.created_by != "vera":
            return None
        command_ids = [r.get("ref") for r in (task.refs or []) if r.get("kind") == "voice_command"]
        if len(command_ids) != 1:
            return None
        row = (await s.execute(select(VoiceCommandRow).where(
            VoiceCommandRow.command_id == command_ids[0], VoiceCommandRow.task_id == task_id
        ))).scalar_one_or_none()
        if row is None:
            return None
        holder, _ = await holder_of(s, room, task_id, task)
        source = row.source or {}
        delivery = source.get("delivery", {})
        route = source.get("result_route")
        if route not in ("external_executor", "executor"):
            route = "external_executor" if external_facts(row.instruction) else "executor"
        receipt = delivery.get("message_id")
        receipt = str(receipt) if type(receipt) is int and receipt > 0 and delivery.get("state") == "sent" else None
        receipt_time = delivery.get("settled_at") or source.get("result_sent_at")
        receipt_time = parse_since(receipt_time) if isinstance(receipt_time, str) else None
        reason = source.get("result_review_reason")
        if reason not in ("delivery_cutoff", "missing_final_text", "legacy_brain_final", "source_unverified",
                          "execution_ended_without_delivery", "unverified_external_result", "stale_unopened"):
            reason = None
        verified = (source.get("guard_version") == GUARD_VERSION
                    and source.get("guard") == "own"
                    and not source_uncertain(row.confidence, source.get("doubts") or []))
        recipient = "dot"
        for ref in task.refs or []:
            value = str(ref.get("ref", ""))
            if value.startswith("result_recipient:"):
                recipient = value.split(":", 1)[1].strip().lower()
        result = {
            "command_id": row.command_id, "source_verified": verified,
            "route": route,
            "room_status": task.status, "handoff_offered_to": task.pending_handoff_to,
            "accepted_executor": holder, "help_state": row.help_state,
            "delivery_owner": delivery.get("owner") if delivery.get("owner") in ("brain", "executor") else None,
            "delivery_state": delivery.get("state") if delivery.get("state") in ("claimed", "unknown", "error", "sent") else "not_started",
            "provider": None, "model": None,
            "receipt": receipt,
            "receipt_at": receipt_time.isoformat() + "Z" if receipt_time else None,
            "review_reason": reason,
        }
    final = await executor_result(room, task_id)
    result["prepared_executor"] = final[0] if final else None
    result["result_ready"] = final is not None
    result["dispatch_ack"] = await has_reply(
        room=room, in_reply_to=f"dispatch-{task_id}", from_agent=recipient, status="ack")
    return result
