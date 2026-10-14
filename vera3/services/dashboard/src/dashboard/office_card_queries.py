"""The single extra batched query of Office card details: status transitions in the window."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from vera_shared.db.models_room import RoomTaskEventRow

from dashboard.office_card_details import TRANSITION_KINDS


async def load_transitions(s: AsyncSession, now: datetime,
                           window: timedelta) -> list[RoomTaskEventRow]:
    ev = RoomTaskEventRow
    result = await s.execute(select(ev).where(
        ev.kind.in_(TRANSITION_KINDS), ev.at >= now - window, ev.at <= now))
    return list(result.scalars().all())
