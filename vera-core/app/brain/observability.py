"""Single observability endpoint — replaces scattered v2 dashboard tabs.

GET /api/observability returns one big snapshot of Vera's current state:
  - source intake totals
  - backfill/ingest queue status
  - graph node counts by label
  - identity (active Goals/Values/NoGo/Style)
  - recent decisions w/ their band
  - last 5 errors (any worker)

UI rebuild is intentionally minimal: one page reads this JSON.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import desc, func, select

from vera_shared.db.engine import get_session
from vera_shared.db.models import (BackfillJob, Event, IngestJob, Source)

from app.brain import identity as ID
from app.config import get_settings
from app.dashboard.auth import require_owner

router = APIRouter(prefix="/api/observability")


@router.get("")
async def snapshot(_=Depends(require_owner)) -> dict:
    async with get_session() as s:
        n_events = (await s.execute(
            select(func.count()).select_from(Event)
        )).scalar() or 0
        per_src = dict((await s.execute(
            select(Event.source, func.count()).group_by(Event.source)
        )).all())
        recent_events = [
            {"id": e.id, "source": e.source, "category": e.category,
             "preview": (e.content_text or "")[:120],
             "occurred_at": e.occurred_at.isoformat()
                            if e.occurred_at else None,
             "status": e.triage_status}
            for e in (await s.execute(
                select(Event).order_by(desc(Event.id)).limit(10)
            )).scalars().all()
        ]
        ij_status = dict((await s.execute(
            select(IngestJob.status, func.count())
            .group_by(IngestJob.status)
        )).all())
        bf = [
            {"id": j.id, "source": j.source_name, "since": j.since.isoformat()
                              if j.since else None,
             "status": j.status, "events_ingested": j.events_ingested,
             "last_error": j.last_error}
            for j in (await s.execute(
                select(BackfillJob).order_by(desc(BackfillJob.id)).limit(10)
            )).scalars().all()
        ]
        sources = [{"name": r.name, "type": r.type, "enabled": r.enabled,
                     "intake_count": r.intake_count}
                   for r in (await s.execute(select(Source))).scalars().all()]

    graph_counts = await _graph_counts()
    identity = await ID.list_active()

    return {
        "events": {"total": n_events, "by_source": per_src,
                    "recent": recent_events},
        "ingest_queue": ij_status,
        "backfill_jobs": bf,
        "sources": sources,
        "graph": graph_counts,
        "identity": {k: len(v) for k, v in identity.items()},
        "identity_detail": identity,
    }


async def _graph_counts() -> dict:
    from app.graph.client import get_graphiti
    client = await get_graphiti()
    db = get_settings().neo4j_database
    out: dict[str, int] = {}
    async with client.driver.session(database=db) as ses:
        for label in ("Event", "Person", "Account", "Container",
                       "Pattern", "Goal", "Value", "NoGo", "Style"):
            r = await ses.run(f"MATCH (n:{label}) RETURN count(n) AS n")
            row = await r.single()
            out[label] = int(row["n"]) if row else 0
    return out
