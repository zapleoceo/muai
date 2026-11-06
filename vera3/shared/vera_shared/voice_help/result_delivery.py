"""Atomic executor reservation and receipts, without Telegram I/O."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import update

from vera_shared.db.engine import get_session
from vera_shared.db.models_voice import VoiceCommandRow
from vera_shared.voice_help.delivery_claim import CAS_TRIES, KEY, update_source
from vera_shared.voice_help.fact_route import external_facts

REVIEW = "result_review"
CLAIM_TIMEOUT = timedelta(minutes=5)


async def reserve_result(command_id: str, now: datetime) -> dict[str, Any] | None:
    """None wins; otherwise return the existing reservation. CAS includes queue state."""
    for _ in range(CAS_TRIES):
        async with get_session() as s:
            row = await s.get(VoiceCommandRow, command_id)
            if row is None:
                return {"owner": "?", "state": "unknown"}
            old, state = row.source or {}, row.help_state
            current = old.get(KEY)
            if current and current.get("state") != "error":
                return current
            if state not in ("opened", "escalated", "reminded", "taken", "result_error"):
                return {"owner": "?", "state": "unknown"}
            guard = VoiceCommandRow.source.is_(None) if row.source is None else VoiceCommandRow.source == old
            res = await s.execute(update(VoiceCommandRow).where(
                VoiceCommandRow.command_id == command_id, guard,
                VoiceCommandRow.help_state == state).values(
                    source={**old, "result_route": "external_executor" if external_facts(row.instruction) else "executor",
                            KEY: {"owner": "executor", "state": "claimed",
                                        "at": now.isoformat()}},
                    help_state="result_pending", updated_at=now))
            if res.rowcount == 1:
                return None
    return {"owner": "?", "state": "unknown"}


async def review_result(command_id: str, reason: str, now: datetime) -> None:
    """Keep legacy receipts and prepared results without replaying owner messages."""
    async with get_session() as s:
        row = await s.get(VoiceCommandRow, command_id)
        state = row.help_state if row else None
        status = "opened" if row and row.status in ("pending", "processing") else (row.status if row else None)
    if state and state not in ("closed", "result_pending", "result_unknown"):
        await update_source(command_id, {"result_review_reason": reason},
                            only_state=state, help_state=REVIEW, status=status, updated_at=now)


async def cancel_help(command_id: str, state: str, now: datetime) -> bool:
    """Execution cancellation is terminal, but is never a delivery receipt."""
    if state not in ("opened", "taken", "escalated", "reminded", "result_error"):
        return False
    return await update_source(command_id, {"execution_outcome": "cancelled"},
                               only_state=state, help_state="cancelled", status="cancelled",
                               instruction="", updated_at=now)


def stale_result_claim(row: VoiceCommandRow, now: datetime) -> bool:
    raw = (row.source or {}).get(KEY, {}).get("at")
    try:
        at = datetime.fromisoformat(raw)
        return now - at >= CLAIM_TIMEOUT
    except (ValueError, TypeError):
        return True


async def store_result_receipt(command_id: str, message_id: int, now: datetime) -> bool:
    """Commit receipt and terminal queue state together; late real ACK resolves unknown."""
    if type(message_id) is not int or message_id <= 0:
        return False
    for _ in range(CAS_TRIES):
        async with get_session() as s:
            row = await s.get(VoiceCommandRow, command_id)
            if row is None:
                return False
            old = row.source or {}
            current = old.get(KEY, {})
            if current.get("owner") != "executor":
                return False
            if row.help_state == "closed":
                return current.get("state") == "sent" and current.get("message_id") == message_id
            if row.help_state not in ("result_pending", "result_unknown"):
                return False
            if current.get("state") not in ("claimed", "unknown", "sent"):
                return False
            receipt = {**current, "state": "sent", "message_id": message_id,
                       "settled_at": now.isoformat()}
            res = await s.execute(update(VoiceCommandRow).where(
                VoiceCommandRow.command_id == command_id,
                VoiceCommandRow.source == old, VoiceCommandRow.help_state == row.help_state
            ).values(source={**old, KEY: receipt, "result_message_id": message_id,
                             "result_sent_at": now.isoformat()},
                     help_state="closed", status="done", answered_at=now,
                     instruction="", updated_at=now))
            if res.rowcount == 1:
                return True
    return False
