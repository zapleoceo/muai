"""Действия владельца, которые двигают зависшую задачу: продолжить, отложить, отменить.

Как и пауза (`owner_actions`), идут без аренды и fencing-токена: владелец — не держатель,
а единственный, кто вправе распорядиться любой задачей. Каждое действие — одна запись
журнала с `data.by="owner"` и каналом (`via`); повтор того же действия ничего не пишет.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from vera_shared.db.engine import get_session
from vera_shared.room.owner_actions import FINAL_STATUSES, OWNER_AGENT, _clean_reason
from vera_shared.room.owner_block import BY_OWNER, pause_state
from vera_shared.room.stall_rules import SNOOZE
from vera_shared.room.task_cancel import apply_cancel
from vera_shared.room.task_events import record_event
from vera_shared.room.task_view import task_dict
from vera_shared.room.tasks import TaskBusy, TaskNotFound, _locked
from vera_shared.timeutil import utc_naive_now
from vera_shared.voice_help.attest_state import attestation_for

SNOOZE_ACTION = "owner_snooze"


def _who(actor: str, via: str) -> dict[str, Any]:
    return {"by": BY_OWNER, "actor": actor, "via": via}


async def cancel_by_owner(*, room: str, task_id: str, reason: str,
                          actor: str = OWNER_AGENT, via: str = "dashboard") -> dict[str, Any]:
    """Отмена любой незавершённой задачи, в том числе под чужой арендой: её держатель
    при следующей записи получит StaleLease. Аттестованная задача — только через dot."""
    reason = _clean_reason(reason)
    async with get_session() as s:
        row = await _locked(s, room, task_id)
        if row is None:
            raise TaskNotFound(room, task_id)
        if row.status == "cancelled":
            return {**task_dict(row), "changed": False}
        if row.status == "done":
            raise TaskBusy(f"task {task_id!r} is already done")
        if await attestation_for(s, room, task_id):
            raise TaskBusy("attested task is cancelled through dot, not by the owner button")
        await apply_cancel(s, row, agent=OWNER_AGENT, fencing_token=row.fencing_token,
                           reason=reason, extra=_who(actor, via))
        await s.refresh(row)
        return {**task_dict(row), "changed": True}


async def continue_by_owner(*, room: str, task_id: str, reason: str,
                            actor: str = OWNER_AGENT, via: str = "dashboard") -> dict[str, Any]:
    """«Продолжить»: задача возвращается в пул (`open`, `auto_pickup`), пауза владельца
    снимается. Чужую живую аренду не трогает — у задачи уже есть исполнитель."""
    reason = _clean_reason(reason)
    now = utc_naive_now()
    async with get_session() as s:
        row = await _locked(s, room, task_id)
        if row is None:
            raise TaskNotFound(room, task_id)
        if row.status in FINAL_STATUSES:
            raise TaskBusy(f"task {task_id!r} is already {row.status}")
        if row.lease_holder and row.lease_until and row.lease_until > now:
            raise TaskBusy(f"task {task_id!r} is held by {row.lease_holder}")
        paused = (await pause_state(s, room, task_id))[BY_OWNER]
        reopen = row.status != "open" or not row.auto_pickup
        if paused:
            await record_event(s, room=room, task_id=task_id, kind="resumed", agent=OWNER_AGENT,
                               fencing_token=row.fencing_token, text=reason,
                               data={**_who(actor, via), "reason": reason})
        if reopen:
            prior = row.status
            row.status, row.auto_pickup = "open", True
            row.lease_holder, row.lease_until = None, None
            await record_event(s, room=room, task_id=task_id, kind="unblocked", agent=OWNER_AGENT,
                               fencing_token=row.fencing_token, text=reason,
                               data={**_who(actor, via), "reason": reason,
                                     "prior_status": prior, "auto_pickup": True})
        if paused or reopen:
            row.updated_at = now
            await s.flush()  # refresh ниже перечитывает строку: без flush правка пропала бы
        await s.refresh(row)
        return {**task_dict(row), "changed": paused or reopen}


async def snooze_by_owner(*, room: str, task_id: str, reason: str,
                          span: timedelta = SNOOZE, actor: str = OWNER_AGENT,
                          via: str = "dashboard") -> dict[str, Any]:
    """«Ждать»: задача уходит с пульта до `until`; статус и аренда не меняются,
    `updated_at` тоже — иначе отсрочка молча удлинялась бы на порог зависания."""
    reason = _clean_reason(reason)
    until = utc_naive_now() + span
    async with get_session() as s:
        row = await _locked(s, room, task_id)
        if row is None:
            raise TaskNotFound(room, task_id)
        if row.status in FINAL_STATUSES:
            raise TaskBusy(f"task {task_id!r} is already {row.status}")
        await record_event(s, room=room, task_id=task_id, kind="watchdog_action",
                           agent=OWNER_AGENT, fencing_token=row.fencing_token, text=reason,
                           data={**_who(actor, via), "action": SNOOZE_ACTION,
                                 "until": until.isoformat(), "reason": reason})
        await s.refresh(row)
        return {**task_dict(row), "changed": True, "snoozed_until": until.isoformat()}
