"""DB-backed pending followup state. Set on 'Свой ответ' click; consumed
by the next DM message from owner within TTL. Survives restarts."""
from datetime import datetime, timedelta

from sqlalchemy import delete, select

from vera_shared.db.engine import get_session
from vera_shared.db.models import PendingFollowup

_TTL = timedelta(minutes=5)


async def set_pending(user_id: int, event_id: int) -> None:
    async with get_session() as s:
        row = await s.get(PendingFollowup, user_id)
        if row is None:
            s.add(PendingFollowup(user_id=user_id, event_id=event_id))
        else:
            row.event_id = event_id
            row.created_at = datetime.utcnow()
        await s.commit()


async def pop_pending(user_id: int) -> int | None:
    async with get_session() as s:
        row = await s.get(PendingFollowup, user_id)
        if row is None:
            return None
        if datetime.utcnow() - row.created_at > _TTL:
            await s.delete(row)
            await s.commit()
            return None
        eid = row.event_id
        await s.delete(row)
        await s.commit()
        return eid


async def peek(user_id: int) -> int | None:
    """Non-destructive lookup — used to display state in dashboard."""
    async with get_session() as s:
        row = await s.get(PendingFollowup, user_id)
        if row is None or datetime.utcnow() - row.created_at > _TTL:
            return None
        return row.event_id
