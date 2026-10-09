"""Взял ли кто-то срочную задачу — по журналу комнаты, и что делать, если нет.

Держатель — первый `claimed`/`handoff_accept` в `room_task_events`, а не
текущий `lease_holder`: исполнитель мог взять и уже отпустить задачу между
двумя проходами, и «Взял» владелец должен получить и тогда. Двух
исполнителей не бывает: `claim` под блокировкой строки и fencing_token.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskEventRow, RoomTaskRow
from vera_shared.voice_help.policy import ESCALATE_AFTER, REMIND_AFTER

TAKEN, ESCALATE, REMIND, CLOSED = "taken", "escalate", "remind", "closed"
FINAL_TASK_STATUSES = ("done", "cancelled")


async def task_holder(room: str, task_id: str) -> tuple[str | None, str | None]:
    """→ (кто взял первым или None, статус задачи или None, если задачи нет)."""
    ev = RoomTaskEventRow
    async with get_session() as s:
        agent = (await s.execute(
            select(ev.agent).where(ev.room == room, ev.task_id == task_id,
                                   ev.kind.in_(("claimed", "handoff_accept")))
            .order_by(ev.id).limit(1))).scalar_one_or_none()
        row = await s.get(RoomTaskRow, (room, task_id))
    return agent, (row.status if row is not None else None)


def next_step(state: str, elapsed: timedelta, holder: str | None,
              task_status: str | None) -> str | None:
    if holder:
        return TAKEN
    if task_status is None or task_status in FINAL_TASK_STATUSES:
        return CLOSED
    if state == "opened" and elapsed >= ESCALATE_AFTER:
        return ESCALATE
    if state == "escalated" and elapsed >= REMIND_AFTER:
        return REMIND
    return None
