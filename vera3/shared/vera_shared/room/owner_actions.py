"""Защищённые действия владельца над задачей: пауза и снятие паузы (дизайн §2.7 S5).

Без аренды и fencing-токена: владелец не держатель. Пишутся существующие виды
`paused`/`resumed` с `data.by="owner"`; статус задачи не меняется. Держатель снять
эту паузу не может (`OwnerBlock` в `set_state`), только `resume_by_owner`.
"""
from __future__ import annotations

from typing import Any

from vera_shared.db.engine import get_session
from vera_shared.room.owner_block import BY_OWNER, pause_state
from vera_shared.room.task_events import record_event
from vera_shared.room.tasks import TaskBusy, TaskNotFound, _locked, task_dict
from vera_shared.timeutil import utc_naive_now

FINAL_STATUSES = ("done", "cancelled")
OWNER_AGENT = "owner"
MAX_REASON = 2000


def _clean_reason(reason: str) -> str:
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("reason must not be empty")
    if len(reason) > MAX_REASON:
        raise ValueError(f"reason is longer than {MAX_REASON} characters")
    return reason


async def _set_owner_pause(room: str, task_id: str, reason: str, *, paused: bool,
                           actor: str) -> dict[str, Any]:
    reason = _clean_reason(reason)
    kind = "paused" if paused else "resumed"
    async with get_session() as s:
        row = await _locked(s, room, task_id)
        if row is None:
            raise TaskNotFound(room, task_id)
        if row.status in FINAL_STATUSES:
            raise TaskBusy(f"task {task_id!r} is already {row.status}")
        changed = (await pause_state(s, room, task_id))[BY_OWNER] != paused
        if changed:
            row.updated_at = utc_naive_now()
            await record_event(s, room=room, task_id=task_id, kind=kind, agent=OWNER_AGENT,
                               fencing_token=row.fencing_token, text=reason,
                               data={"by": BY_OWNER, "actor": actor, "reason": reason,
                                     "via": "dashboard"})
        await s.refresh(row)
        return {**task_dict(row), "changed": changed, "owner_paused": paused}


async def pause_by_owner(*, room: str, task_id: str, reason: str,
                         actor: str = OWNER_AGENT) -> dict[str, Any]:
    """Пауза владельца; повтор при уже стоящей паузе — без новой записи."""
    return await _set_owner_pause(room, task_id, reason, paused=True, actor=actor)


async def resume_by_owner(*, room: str, task_id: str, reason: str,
                          actor: str = OWNER_AGENT) -> dict[str, Any]:
    """Снимает только паузу владельца; пауза держателя остаётся (ST7)."""
    return await _set_owner_pause(room, task_id, reason, paused=False, actor=actor)
