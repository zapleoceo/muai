"""Трекер задач, шаг 2: прогресс, смена состояния и история. Всё — под живой арендой."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from vera_shared.db.engine import get_session
from vera_shared.room.task_events import list_events, record_event
from vera_shared.room.tasks import _locked, _require_lease, task_dict
from vera_shared.timeutil import utc_naive_now

STATE_KINDS = ("paused", "review", "resumed", "blocked", "unblocked")
# paused/review живут только в журнале: статус задачи остаётся прежним
STATE_TO_STATUS = {"blocked": "blocked", "unblocked": "in_progress", "resumed": "in_progress"}
MIN_CHECKPOINT_S, MAX_CHECKPOINT_S = 60, 86_400


async def progress(*, room: str, task_id: str, agent: str, fencing_token: int, result: str,
                   next_checkpoint_seconds: int | None = None) -> dict[str, Any]:
    if next_checkpoint_seconds is not None and not (
            MIN_CHECKPOINT_S <= next_checkpoint_seconds <= MAX_CHECKPOINT_S):
        raise ValueError(f"next_checkpoint_seconds must be {MIN_CHECKPOINT_S}.."
                         f"{MAX_CHECKPOINT_S}")
    async with get_session() as s:
        row = _require_lease(await _locked(s, room, task_id), room, task_id, agent,
                             fencing_token)
        now = utc_naive_now()
        row.last_progress_at, row.last_progress_text, row.updated_at = now, result, now
        row.waiting_until, row.waiting_reason = None, None  # результат закрывает ожидание
        if next_checkpoint_seconds is not None:
            row.next_checkpoint_at = now + timedelta(seconds=next_checkpoint_seconds)
        await record_event(s, room=room, task_id=task_id, kind="progress", agent=agent,
                           session=row.holder_session, account=row.holder_account,
                           fencing_token=fencing_token, text=result,
                           data={"next_checkpoint_seconds": next_checkpoint_seconds})
        await s.refresh(row)
        return task_dict(row)


async def set_state(*, room: str, task_id: str, agent: str, fencing_token: int, state: str,
                    reason: str) -> dict[str, Any]:
    if state not in STATE_KINDS:
        raise ValueError(f"state must be one of {', '.join(STATE_KINDS)}")
    async with get_session() as s:
        row = _require_lease(await _locked(s, room, task_id), room, task_id, agent,
                             fencing_token)
        if state in STATE_TO_STATUS:
            row.status = STATE_TO_STATUS[state]
        row.updated_at = utc_naive_now()
        await record_event(s, room=room, task_id=task_id, kind=state, agent=agent,
                           session=row.holder_session, account=row.holder_account,
                           fencing_token=fencing_token, text=reason)
        await s.refresh(row)
        return task_dict(row)


async def history(*, room: str, task_id: str, since_id: int | None,
                  limit: int) -> list[dict[str, Any]]:
    async with get_session() as s:
        return await list_events(s, room=room, task_id=task_id, since_id=since_id,
                                 limit=limit)
