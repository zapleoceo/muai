"""Read the append-only attestation event when enforcing delivery scope."""
from __future__ import annotations

from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskEventRow


async def is_attested(room: str, task_id: str) -> bool:
    ev = RoomTaskEventRow
    async with get_session() as s:
        found = await s.execute(select(ev.id).where(
            ev.room == room, ev.task_id == task_id,
            ev.kind == "delegated_attestation").limit(1))
        return found.first() is not None
