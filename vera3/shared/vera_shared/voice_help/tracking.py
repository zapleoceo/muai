"""Взял ли кто-то срочную задачу — по журналу комнаты, и что делать, если нет.

«Взял» — только про ТЕКУЩЕГО держателя: `room_tasks.lease_holder` не `vera`, аренда
жива (`lease_until` > сейчас), и он получил задачу через `handoff_accept` на текущем
`fencing_token` после последнего предложения. Квитанция доставки (ack), старый
`claimed` агента с истёкшей арендой и сам диспетчер («Взял: vera» не бывает) — не в счёт.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskEventRow, RoomTaskRow
from vera_shared.timeutil import utc_naive_now
from vera_shared.voice_help.policy import AGENT, ESCALATE_AFTER, REMIND_AFTER

TAKEN, ESCALATE, REMIND, CLOSED = "taken", "escalate", "remind", "closed"
FINAL_TASK_STATUSES = ("done", "cancelled")


def _is_offer(data: object) -> bool:
    return isinstance(data, dict) and data.get("action") == "offer"


async def task_holder(room: str, task_id: str) -> tuple[str | None, str | None]:
    """→ (живой держатель, принявший передачу, или None; статус задачи или None, если нет)."""
    ev = RoomTaskEventRow
    async with get_session() as s:
        row = await s.get(RoomTaskRow, (room, task_id))
        if row is None:
            return None, None
        holder = row.lease_holder
        if (not holder or holder == AGENT or row.lease_until is None
                or row.lease_until <= utc_naive_now()):
            return None, row.status
        events = (await s.execute(
            select(ev.id, ev.kind, ev.agent, ev.fencing_token, ev.data).where(
                ev.room == room, ev.task_id == task_id,
                ev.kind.in_(("handoff_offer", "handoff_accept"))).order_by(ev.id))).all()
    last_offer = max((e.id for e in events
                      if e.kind == "handoff_offer" and _is_offer(e.data)), default=0)
    accepted = any(e.kind == "handoff_accept" and e.agent == holder and e.id > last_offer
                   and e.fencing_token == row.fencing_token for e in events)
    return (holder if accepted else None), row.status


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
