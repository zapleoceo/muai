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
