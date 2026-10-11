"""Read the append-only attestation event when enforcing delivery scope."""
from __future__ import annotations

from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskEventRow


async def attestation_for(s, room: str, task_id: str) -> RoomTaskEventRow | None:
    ev = RoomTaskEventRow
    return (await s.execute(select(ev).where(
        ev.room == room, ev.task_id == task_id,
        ev.kind == "delegated_attestation").order_by(ev.id).limit(1)
    )).scalar_one_or_none()


async def is_attested(room: str, task_id: str) -> bool:
    async with get_session() as s:
        return await attestation_for(s, room, task_id) is not None
