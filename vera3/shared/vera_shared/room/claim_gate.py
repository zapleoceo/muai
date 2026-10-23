"""Ворота новой аренды (дизайн §3): одна проверка для `claim`, `next` и `accept`.

Зовётся под блокировкой строки задачи. Возвращает первый отказ или None.
Продление своей живой аренды ворота не проходит (S1). Коды и режимы — в
`docs/agent-room.md`; в режиме `off` действует только то, что одобрено владельцем,
остальное (истёкшая аренда -> `held_expired`/`recovery_pending`, пауза держателя для `claim`)
остаётся статус-кво (F1, F2, F8).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.models_room import RoomTaskQuestionRow, RoomTaskRow
from vera_shared.room.owner_block import BY_HOLDER, BY_OWNER, owner_wait, pause_state
from vera_shared.room.question_view import load_questions, question_dict
from vera_shared.room.single_executor import Mode, single_executor_mode

Via = Literal["claim", "next", "accept"]
FINAL_STATUSES = ("done", "cancelled")


@dataclass(frozen=True)
class GateRefusal:
    code: str
    detail: str

    @property
    def message(self) -> str:
        return f"{self.code}: {self.detail}"


def _live(row: RoomTaskRow, now: datetime) -> bool:
    return bool(row.lease_holder and row.lease_until and row.lease_until > now)


async def _question_states(s: AsyncSession, row: RoomTaskRow) -> set[str]:
    rows = (await s.execute(select(RoomTaskQuestionRow.status).where(
        RoomTaskQuestionRow.room == row.room, RoomTaskQuestionRow.task_id == row.task_id,
        RoomTaskQuestionRow.status.in_(("open", "answered"))).distinct())).scalars().all()
    return set(rows)


def _lease_refusal(row: RoomTaskRow, now: datetime, *, agent: str, via: Via,
                   sender: str | None, mode: Mode) -> GateRefusal | None:
    tid = row.task_id
    if via == "accept":
        # §3.1: принять можно, только пока аренда отправителя жива и предложена именно агенту
        if row.pending_handoff_to != agent:
            return GateRefusal("held", f"handoff of {tid!r} is not offered to {agent}")
        if mode != "off" and not (_live(row, now) and row.lease_holder == sender):
            return GateRefusal("held_expired", f"the lease of the sender of {tid!r} expired; "
                               "the sender renews it and offers again")
        return None
    if _live(row, now):
        if row.lease_holder != agent:
            return GateRefusal("held", f"task {tid!r} is held by {row.lease_holder} "
                               f"until {row.lease_until.isoformat()}Z")  # type: ignore[union-attr]
        return None
    if mode != "off" and row.lease_holder:
        # без ключа (в B1 ключей ещё нет) истёкшая аренда — recovery_pending; с хешем — held_expired
        hashed = bool(getattr(row, "lease_key_hash", None))
        return GateRefusal("held_expired" if hashed else "recovery_pending",
                           f"task {tid!r}: the previous holder's lease expired; "
                           "only the owner's recover lets a new run continue")
    return None


async def check_new_lease(s: AsyncSession, row: RoomTaskRow, now: datetime, *, agent: str,
                          via: Via, sender: str | None = None,
                          mode: Mode | None = None) -> GateRefusal | None:
    """Первый отказ новой аренды или None. `mode=None` — режим флага (в B1 всегда `off`)."""
    mode = mode or single_executor_mode()
    tid = row.task_id
    if row.status in FINAL_STATUSES:
        return GateRefusal("final", f"task {tid!r} is {row.status}; open a new task_id")
    if (refusal := _lease_refusal(row, now, agent=agent, via=via, sender=sender,
                                  mode=mode)) is not None:
        return refusal
    if getattr(row, "recovery_hold", False):
        return GateRefusal("recovery_hold", f"task {tid!r} awaits the owner's start decision "
                           "after recover")
    pauses = await pause_state(s, row.room, tid)
    if pauses[BY_OWNER]:
        return GateRefusal("paused", f"task {tid!r} is paused by the owner; "
                           "only the owner's resume allows a new lease")
    # пауза держателя: next/accept отказывали и раньше; для claim это новое и только для `on`
    if pauses[BY_HOLDER] and (via != "claim" or mode != "off"):
        return GateRefusal("paused", f"task {tid!r} is paused; resume it before a new lease")
    states = await _question_states(s, row)
    if "open" in states:
        return GateRefusal("owner_question", f"task {tid!r} has a question awaiting the owner")
    if "answered" in states and via != "claim":
        return GateRefusal("answer_unacked", f"task {tid!r} has an answer not yet acked by "
                           "the executor; only an explicit claim continues it")
    if owner_wait(row):
        return GateRefusal("owner_wait", f"task {tid!r} waits for the owner's decision; "
                           "only the owner's wait/resolve lifts it")
    return None


async def answered_questions(s: AsyncSession, row: RoomTaskRow) -> list[dict[str, Any]]:
    """Вопросы в `answered` с ответами — отдаются в выдаче `claim` (§2.8)."""
    return [{"qid": q.qid, "question": q.question,
             "answers": question_dict(q, answers)["answers"]}
            for q, answers in await load_questions(s, row.room, row.task_id)
            if q.status == "answered"]
