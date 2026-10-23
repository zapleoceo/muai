"""Защищённые действия владельца над задачей: пауза, resume, wait/resolve, закрытие вопроса (§2.7 S5, §2.8).

Пауза и wait/resolve — без аренды и fencing-токена: владелец не держатель. Пишутся существующие виды
`paused`/`resumed` с `data.by="owner"`; статус задачи не меняется. Держатель снять
эту паузу не может (`OwnerBlock` в `set_state`), только `resume_by_owner`.
"""
from __future__ import annotations

from typing import Any

from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskQuestionRow
from vera_shared.room.owner_block import BY_OWNER, clear_all_wait, owner_wait, pause_state
from vera_shared.room.question_view import load_questions, question_dict
from vera_shared.room.questions import QuestionNotFound, QuestionState
from vera_shared.room.task_events import record_event
from vera_shared.room.tasks import StaleLease, TaskBusy, TaskNotFound, _locked, task_dict
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


async def _answers_of(s: Any, q: RoomTaskQuestionRow) -> list[Any]:
    return next(a for qq, a in await load_questions(s, q.room, q.task_id) if qq.qid == q.qid)


async def resolve_wait_by_owner(*, room: str, task_id: str, reason: str,
                                actor: str = OWNER_AGENT) -> dict[str, Any]:
    """Снимает ожидание владельца (S5 w): событие `wait_resolved{by:owner}`, статус не меняется.

    Аренда не нужна. Если ожидания владельца нет — записи нет (changed=False)."""
    reason = _clean_reason(reason)
    async with get_session() as s:
        row = await _locked(s, room, task_id)
        if row is None:
            raise TaskNotFound(room, task_id)
        if row.status in FINAL_STATUSES:
            raise TaskBusy(f"task {task_id!r} is already {row.status}")
        changed = owner_wait(row)
        if changed:
            was = {"decision_owner": row.waiting_decision_owner,
                   "reason": row.waiting_reason,
                   "until": row.waiting_until.isoformat() if row.waiting_until else None}
            clear_all_wait(row)
            row.updated_at = utc_naive_now()
            await record_event(s, room=room, task_id=task_id, kind="wait_resolved",
                               agent=OWNER_AGENT, fencing_token=row.fencing_token, text=reason,
                               data={"by": BY_OWNER, "actor": actor, "reason": reason,
                                     "via": "dashboard", "was": was})
        await s.refresh(row)
        return {**task_dict(row), "changed": changed}


async def close_question_by_owner(*, room: str, task_id: str, qid: int, reason: str,
                                  expected_fencing_token: int,
                                  actor: str = OWNER_AGENT) -> dict[str, Any]:
    """Закрывает вопрос владельцем (§2.8): `withdrawn`, `ack_by=owner`, статус задачи не меняется.

    Текст вопроса и ответы остаются. Живая аренда любого держателя -> TaskBusy (409): сначала
    ответ владельца и `ack` держателя. Неверный fencing -> StaleLease. Повтор — без записи."""
    reason = _clean_reason(reason)
    async with get_session() as s:
        row = await _locked(s, room, task_id)
        if row is None:
            raise TaskNotFound(room, task_id)
        if row.fencing_token != expected_fencing_token:
            raise StaleLease(f"fencing_token {expected_fencing_token} is stale "
                             f"(current {row.fencing_token})")
        if row.status in FINAL_STATUSES:
            raise TaskBusy(f"task {task_id!r} is already {row.status}")
        if row.lease_holder and row.lease_until and row.lease_until > utc_naive_now():
            raise TaskBusy(f"task {task_id!r} is held by {row.lease_holder}; answer the "
                           "question and let the holder ack it, or wait for the lease to end")
        q = await s.get(RoomTaskQuestionRow, qid, with_for_update=True)
        if q is None or (q.room, q.task_id) != (room, task_id):
            raise QuestionNotFound(qid)
        if q.status == "withdrawn" and q.ack_by == OWNER_AGENT:
            return {"question": question_dict(q, await _answers_of(s, q)), "changed": False}
        if q.status not in ("open", "answered"):
            raise QuestionState(f"question {qid} is {q.status}; nothing to close")
        prior = q.status
        q.status, q.ack_by = "withdrawn", OWNER_AGENT
        row.updated_at = utc_naive_now()
        await record_event(s, room=room, task_id=task_id, kind="question_closed",
                           agent=OWNER_AGENT, fencing_token=row.fencing_token, text=reason,
                           data={"by": BY_OWNER, "actor": actor, "qid": qid, "reason": reason,
                                 "was": prior, "via": "dashboard"})
        await s.flush()
        return {"question": question_dict(q, await _answers_of(s, q)), "changed": True}
