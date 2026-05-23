"""Background workers: ingest queue + backfill queue.

Started from main.lifespan via bg.spawn(). Two independent loops:
  - ingest: pops oldest pending IngestJob, runs deep-extract on its event
  - backfill: pops oldest pending BackfillJob, drains the source's
    backfill iterator, saving events through brain.ingest

Both loops sleep when the queue is empty and exit cleanly on cancel.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models import BackfillJob, IngestJob

log = logging.getLogger(__name__)

_INGEST_IDLE_SEC = 5.0
_BACKFILL_IDLE_SEC = 30.0


async def ingest_loop() -> None:
    while True:
        try:
            job = await _claim_ingest_job()
            if job is None:
                await asyncio.sleep(_INGEST_IDLE_SEC)
                continue
            await _run_ingest(job)
        except asyncio.CancelledError:
            log.info("ingest_loop cancelled")
            raise
        except Exception as exc:
            log.exception("ingest_loop iteration failed: %s", exc)
            await asyncio.sleep(_INGEST_IDLE_SEC)


async def backfill_loop() -> None:
    while True:
        try:
            job = await _claim_backfill_job()
            if job is None:
                await asyncio.sleep(_BACKFILL_IDLE_SEC)
                continue
            await _run_backfill(job)
        except asyncio.CancelledError:
            log.info("backfill_loop cancelled")
            raise
        except Exception as exc:
            log.exception("backfill_loop iteration failed: %s", exc)
            await asyncio.sleep(_BACKFILL_IDLE_SEC)


async def _claim_ingest_job() -> IngestJob | None:
    async with get_session() as s:
        # Prefer least-attempted jobs first so a rate-limited row doesn't
        # immediately re-claim itself ahead of fresh work.
        row = (await s.execute(
            select(IngestJob).where(IngestJob.status == "pending")
            .order_by(IngestJob.attempts, IngestJob.id).limit(1)
        )).scalar_one_or_none()
        if row is None:
            return None
        row.status = "running"
        row.started_at = datetime.utcnow()
        row.attempts += 1
        await s.commit()
        await s.refresh(row)
        return row


async def _claim_backfill_job() -> BackfillJob | None:
    async with get_session() as s:
        row = (await s.execute(
            select(BackfillJob).where(BackfillJob.status == "pending")
            .order_by(BackfillJob.id).limit(1)
        )).scalar_one_or_none()
        if row is None:
            return None
        row.status = "running"
        row.started_at = datetime.utcnow()
        await s.commit()
        await s.refresh(row)
        return row


_MAX_INGEST_ATTEMPTS = 6
_RATE_LIMIT_BACKOFF_SEC = 60.0


async def _run_ingest(job: IngestJob) -> None:
    from app.brain import ingest as brain_ingest
    try:
        await brain_ingest.deep_extract(job.event_id)
        await _finish(IngestJob, job.id, status="done")
    except Exception as exc:
        msg = str(exc)
        lo = msg.lower()
        is_rate = ("rate limit" in lo or "429" in msg or "quota" in lo
                   or "tokensexhausted" in lo or "tokens available" in lo
                   or "retry after" in lo or isinstance(exc, type(exc))
                   and exc.__class__.__name__ == "TokensExhausted")
        if is_rate and job.attempts < _MAX_INGEST_ATTEMPTS:
            # Extract "retry after Ns" hint from the pool — it's the only
            # honest signal of when keys will replenish.
            import re
            m = re.search(r"retry after (\d+)", msg)
            wait = min(int(m.group(1)) if m else 5, 60)
            log.warning("deep_extract rate-limited event=%s attempt=%s — requeue, sleep %ds",
                         job.event_id, job.attempts, wait)
            await _requeue(IngestJob, job.id)
            await asyncio.sleep(wait)
            return
        log.exception("deep_extract failed for event=%s: %s", job.event_id, exc)
        await _finish(IngestJob, job.id, status="error", error=msg[:500])


async def _requeue(model: type, job_id: int) -> None:
    async with get_session() as s:
        row = await s.get(model, job_id)
        if row is None:
            return
        row.status = "pending"
        row.started_at = None
        await s.commit()


async def _run_backfill(job: BackfillJob) -> None:
    from app.brain import ingest as brain_ingest
    from app.sources.registry import get_source
    try:
        src = await get_source(job.source_name)
        if src is None:
            await _finish(BackfillJob, job.id, status="error",
                          error=f"source '{job.source_name}' not registered")
            return
        n = 0
        async for envelope in src.backfill(job.since):
            await brain_ingest.ingest(envelope)
            n += 1
            if n % 5 == 0:
                await _bump_backfill_count(job.id, n)
        await _bump_backfill_count(job.id, n)
        await _finish(BackfillJob, job.id, status="done")
        log.info("backfill done: source=%s events=%d", job.source_name, n)
    except Exception as exc:
        log.exception("backfill failed for %s: %s", job.source_name, exc)
        await _finish(BackfillJob, job.id, status="error", error=str(exc)[:500])


async def _bump_backfill_count(job_id: int, n: int) -> None:
    async with get_session() as s:
        row = await s.get(BackfillJob, job_id)
        if row:
            row.events_ingested = n
            await s.commit()


async def _finish(model: type, job_id: int, status: str,
                  error: str | None = None) -> None:
    async with get_session() as s:
        row = await s.get(model, job_id)
        if row is None:
            return
        row.status = status
        row.finished_at = datetime.utcnow()
        if error:
            row.last_error = error
        await s.commit()


def start_all() -> None:
    """Called from main.lifespan."""
    from app.common.bg import spawn
    spawn(ingest_loop(), name="ingest_loop")
    spawn(backfill_loop(), name="backfill_loop")
    log.info("jobs.runner: ingest + backfill loops spawned")
