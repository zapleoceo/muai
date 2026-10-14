"""Законное ожидание внешнего события: до срока задача не считается зависшей."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from vera_shared.db.engine import get_session
from vera_shared.room.task_events import record_event
from vera_shared.room.task_fields import clean_decision_owner
from vera_shared.room.tasks import _locked, _require_lease, task_dict
from vera_shared.timeutil import utc_naive_now
from vera_shared.voice_help.attest_state import attestation_for

MIN_WAIT_S, MAX_WAIT_S = 60, 7 * 86_400


async def wait(*, room: str, task_id: str, agent: str, fencing_token: int,
               until_seconds: int, reason: str,
               decision_owner: str | None = None) -> dict[str, Any]:
    if not MIN_WAIT_S <= until_seconds <= MAX_WAIT_S:
        raise ValueError(f"until_seconds must be {MIN_WAIT_S}..{MAX_WAIT_S}")
    owner = clean_decision_owner(decision_owner)
    async with get_session() as s:
        row = _require_lease(await _locked(s, room, task_id), room, task_id, agent,
                             fencing_token)
        if await attestation_for(s, room, task_id):
            raise ValueError("attested task permits only receipt completion")
        now = utc_naive_now()
        row.waiting_until, row.waiting_reason = now + timedelta(seconds=until_seconds), reason
        row.waiting_decision_owner = owner
        row.updated_at = now
        await record_event(s, room=room, task_id=task_id, kind="waiting", agent=agent,
                           session=row.holder_session, account=row.holder_account,
                           fencing_token=fencing_token, text=reason,
                           data={"until_seconds": until_seconds,
                                 **({"decision_owner": owner} if owner else {})})
        await s.refresh(row)
        return task_dict(row)
