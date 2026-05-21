import asyncio
import hmac
import logging

from fastapi import APIRouter, Header, HTTPException
from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models import Event

from app.config import get_settings
from app.triage.dispatcher import schedule_triage

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin")


def _check_internal(secret: str | None) -> None:
    expected = get_settings().internal_secret
    if not secret or not hmac.compare_digest(secret, expected):
        raise HTTPException(401, "invalid X-Internal-Secret")


@router.post("/replay-triage")
async def replay_triage(x_internal_secret: str | None = Header(default=None)) -> dict:
    _check_internal(x_internal_secret)
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
