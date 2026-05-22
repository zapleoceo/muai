import asyncio
import logging

from fastapi import APIRouter, Depends
from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models import Event

from app.dashboard.auth import require_owner
from app.triage.dispatcher import schedule_triage

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin")


@router.post("/expire-stale-events")
async def expire_stale_events(hours: int = 48, _=Depends(require_owner)) -> dict:
    """Move pending|awaiting_user events older than N hours to 'expired'.
    Keeps the dashboard clean and stops accidental re-triage if any code
    path tries to schedule them again."""
    from datetime import datetime, timedelta
    from sqlalchemy import update
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    async with get_session() as session:
        stmt = (
            update(Event)
            .where(Event.triage_status.in_(["pending", "awaiting_user"]))
            .where(Event.occurred_at < cutoff)
            .values(triage_status="expired")
        )
        result = await session.execute(stmt)
        await session.commit()
    return {"ok": True, "expired": result.rowcount or 0, "older_than_hours": hours}


@router.post("/replay-triage")
async def replay_triage(_=Depends(require_owner)) -> dict:
    async with get_session() as session:
        result = await session.execute(
            select(Event.id).where(Event.triage_status == "pending")
            .order_by(Event.id.desc())
        )
        ids = [row[0] for row in result.all()]
    for i in ids:
        schedule_triage(i)
        await asyncio.sleep(0.05)
    return {"ok": True, "scheduled": len(ids)}
