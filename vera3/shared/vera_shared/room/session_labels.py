"""Resolve exclusive session labels used for Room message addressing."""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.models_room import RoomMessageRow

log = logging.getLogger(__name__)


async def _exclusive_labels(s: AsyncSession, room: str, agent: str) -> set[str]:
    claimed = (await s.execute(select(RoomMessageRow.from_session).where(
        RoomMessageRow.room == room, RoomMessageRow.from_agent == agent,
        RoomMessageRow.from_session.is_not(None),
    ).distinct())).scalars().all()
    others = (await s.execute(select(RoomMessageRow.from_session).where(
        RoomMessageRow.room == room, RoomMessageRow.from_agent != agent,
        RoomMessageRow.from_session.is_not(None),
    ).distinct())).scalars().all()
    agents = (await s.execute(select(RoomMessageRow.from_agent).where(
        RoomMessageRow.room == room).distinct())).scalars().all()
    conflicts = set(claimed) & (set(others) | (set(agents) - {agent}))
    if conflicts:
        log.warning("Room session labels conflict in %s (%d labels)", room,
                    len(conflicts))
    return set(claimed) - set(others) - set(agents)
