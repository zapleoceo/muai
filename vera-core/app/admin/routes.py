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


_REPLAYABLE_STATUSES = (
    "pending", "failed", "execute_failed", "auto_failed",
    "expired", "proposal_only",
)


@router.post("/replay-triage")
async def replay_triage(statuses: str | None = None,
                        _=Depends(require_owner)) -> dict:
    """Reschedule triage for stuck events.

    Default: re-runs 'pending' only (safe).
    Pass ?statuses=pending,failed,expired to widen the net.
    Allowed: pending, failed, execute_failed, auto_failed, expired, proposal_only.
    """
    if statuses:
        wanted = [s.strip() for s in statuses.split(",") if s.strip()]
        bad = [s for s in wanted if s not in _REPLAYABLE_STATUSES]
        if bad:
            from fastapi import HTTPException
            raise HTTPException(400, f"unsupported statuses: {bad}; "
                                     f"allowed: {list(_REPLAYABLE_STATUSES)}")
    else:
        wanted = ["pending"]

    async with get_session() as session:
        result = await session.execute(
            select(Event.id)
            .where(Event.triage_status.in_(wanted))
            .order_by(Event.id.desc())
        )
        ids = [row[0] for row in result.all()]
    for i in ids:
        schedule_triage(i)
        await asyncio.sleep(0.05)
    return {"ok": True, "scheduled": len(ids), "statuses": wanted}


@router.post("/reingest-brain")
async def reingest_brain(_=Depends(require_owner)) -> dict:
    """Re-attempt Graphiti episode write for events that triaged but never
    landed in the graph (graphiti_episode_uuid IS NULL).
    Only re-ingests events that are NOT silenced — silenced events were
    judged not worth remembering, no point burning quota on them.
    """
    from app.events.ingest import ingest_episode
    from app.common.bg import spawn
    async with get_session() as session:
        result = await session.execute(
            select(Event)
            .where(Event.graphiti_episode_uuid.is_(None))
            .where(Event.triage_status.notin_(["silenced", "expired"]))
            .order_by(Event.id.desc())
        )
        events = result.scalars().all()
    queued = 0
    for ev in events:
        spawn(ingest_episode(
            ev.id, source=ev.source,
            category=ev.category or "communication",
            content_text=ev.content_text,
            entity_hints=ev.entity_hints or [],
            metadata=ev.metadata_ or {},
            occurred_at=ev.occurred_at,
        ), name=f"reingest-{ev.id}")
        queued += 1
        if queued % 5 == 0:
            await asyncio.sleep(0.1)  # pace the spawn rate
    return {"ok": True, "queued": queued}
