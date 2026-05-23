"""Admin API for backfill + ingest job queues."""
from __future__ import annotations

import logging
from datetime import date, datetime

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import desc, select

from vera_shared.db.engine import get_session
from vera_shared.db.models import BackfillJob, IngestJob

from app.dashboard.auth import require_owner

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/jobs")


def _bf_row(r: BackfillJob) -> dict:
    return {
        "id": r.id, "source_name": r.source_name,
        "since": r.since.isoformat() if r.since else None,
        "status": r.status, "events_ingested": r.events_ingested,
        "last_error": r.last_error,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


@router.get("/backfill")
async def list_backfill(_=Depends(require_owner)) -> list[dict]:
    async with get_session() as s:
        rows = (await s.execute(
            select(BackfillJob).order_by(desc(BackfillJob.id)).limit(50)
        )).scalars().all()
    return [_bf_row(r) for r in rows]


@router.post("/backfill")
async def start_backfill(payload: dict = Body(...),
                          _=Depends(require_owner)) -> dict:
    src = (payload.get("source_name") or "").strip()
    since_raw = payload.get("since")
    if not src or not since_raw:
        raise HTTPException(400, "source_name and since (YYYY-MM-DD) required")
    try:
        since = date.fromisoformat(since_raw)
    except ValueError:
        raise HTTPException(400, "since must be YYYY-MM-DD")
    async with get_session() as s:
        job = BackfillJob(source_name=src, since=since, status="pending")
        s.add(job)
        await s.commit()
        await s.refresh(job)
    log.info("backfill queued: %s since=%s job=%d", src, since, job.id)
    return _bf_row(job)


@router.post("/ingest/reset-errors")
async def reset_errors(max_attempts: int = 6,
                        _=Depends(require_owner)) -> dict:
    """Move rate-limited errors back to pending so the runner retries.
    Only resets rows whose attempts < max_attempts."""
    from sqlalchemy import update
    async with get_session() as s:
        stmt = (update(IngestJob)
                .where(IngestJob.status == "error")
                .where(IngestJob.attempts < max_attempts)
                .values(status="pending", started_at=None, last_error=None))
        r = await s.execute(stmt)
        await s.commit()
    return {"ok": True, "requeued": r.rowcount or 0}


@router.get("/ingest")
async def ingest_status(_=Depends(require_owner)) -> dict:
    """Counts by status — quick health view of the deep-extract queue."""
    from sqlalchemy import func
    async with get_session() as s:
        rows = (await s.execute(
            select(IngestJob.status, func.count())
            .group_by(IngestJob.status)
        )).all()
    return {"by_status": {status: n for status, n in rows},
            "as_of": datetime.utcnow().isoformat()}
