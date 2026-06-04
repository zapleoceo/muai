"""Continuous backfill of historical events into Graphiti (brain).

Survives container rebuilds because it's baked into the image at
/app/scripts/backfill_brain.py (not /tmp). Run as a systemd-managed
docker-exec loop — script is fully resilient on its own:

- Idempotent: only picks events where graphiti_episode_uuid IS NULL.
- Paced: PACE_SECONDS sleep between events so triage/chat still get
  their share of the Gemini RPM window.
- Bounded: each deep_extract is wrapped in asyncio.wait_for(timeout=600).
- BaseException-safe: catches CancelledError too, so SIGTERM during a
  graphiti call still writes progress before exit.
- Writes JSON progress to /data/backfill_progress.json which the
  admin dashboard reads.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, "/app")

from sqlalchemy import select, and_, func

from app.brain.ingest import deep_extract
from vera_shared.db.engine import get_session
from vera_shared.db.models import Event

PACE_SECONDS = int(os.environ.get("BACKFILL_PACE_SECONDS", "30"))
LOOKBACK_DAYS = int(os.environ.get("BACKFILL_LOOKBACK_DAYS", "30"))
PER_CALL_TIMEOUT = float(os.environ.get("BACKFILL_TIMEOUT_S", "600"))
PROGRESS_FILE = Path(os.environ.get(
    "BACKFILL_PROGRESS_FILE", "/data/backfill_progress.json"
))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("backfill_brain")

_state: dict = {
    "started_at": datetime.utcnow().isoformat() + "Z",
    "total": 0,
    "done": 0,
    "errors": 0,
    "skipped": 0,
    "last_event_id": None,
    "last_ok_at": None,
    "last_error": None,
    "rate_per_min": 0.0,
    "status": "starting",
}
_recent_done: list[float] = []


def save_state() -> None:
    try:
        PROGRESS_FILE.write_text(json.dumps(_state, indent=2))
    except Exception as exc:  # noqa: BLE001
        log.warning("progress write failed: %s", exc)


def _update_rate() -> None:
    now = time.time()
    _recent_done.append(now)
    while _recent_done and now - _recent_done[0] > 300:
        _recent_done.pop(0)
    _state["rate_per_min"] = round(len(_recent_done) / 5, 2)


async def _count_pending() -> tuple[int, int]:
    cutoff = datetime.utcnow() - timedelta(days=LOOKBACK_DAYS)
    async with get_session() as s:
        total = (await s.execute(
            select(func.count(Event.id)).where(Event.occurred_at >= cutoff)
        )).scalar_one()
        done = (await s.execute(
            select(func.count(Event.id)).where(and_(
                Event.occurred_at >= cutoff,
                Event.graphiti_episode_uuid.isnot(None),
            ))
        )).scalar_one()
    return int(total), int(done)


async def _next_batch(limit: int = 20) -> list[int]:
    cutoff = datetime.utcnow() - timedelta(days=LOOKBACK_DAYS)
    async with get_session() as s:
        rows = (await s.execute(
            select(Event.id)
            .where(and_(
                Event.occurred_at >= cutoff,
                Event.graphiti_episode_uuid.is_(None),
                Event.content_text.isnot(None),
                func.length(Event.content_text) > 0,
            ))
            .order_by(Event.occurred_at.desc())
            .limit(limit)
        )).all()
    return [r[0] for r in rows]


async def _process_one(event_id: int) -> bool:
    try:
        await asyncio.wait_for(deep_extract(event_id), timeout=PER_CALL_TIMEOUT)
        _state["done"] += 1
        _state["last_event_id"] = event_id
        _state["last_ok_at"] = datetime.utcnow().isoformat() + "Z"
        _update_rate()
        return True
    except asyncio.TimeoutError:
        _state["errors"] += 1
        _state["last_error"] = f"timeout on event {event_id}"
        log.warning("timeout on event %s", event_id)
        return False
    except BaseException as exc:  # noqa: BLE001 — catch CancelledError too
        _state["errors"] += 1
        _state["last_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        log.warning("deep_extract failed on event %s: %s", event_id, exc)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return False


async def main() -> None:
    log.info(
        "backfill: pace=%ss timeout=%ss lookback=%sd",
        PACE_SECONDS, PER_CALL_TIMEOUT, LOOKBACK_DAYS,
    )
    _state["status"] = "running"
    save_state()
    idle_rounds = 0

    while True:
        try:
            total, done = await _count_pending()
            _state["total"] = total
            # done counter is authoritative from DB, not in-process counter
            _state["done"] = done
            save_state()

            batch = await _next_batch(limit=20)
            if not batch:
                idle_rounds += 1
                _state["status"] = "idle" if idle_rounds < 3 else "caught_up"
                save_state()
                await asyncio.sleep(60)
                continue
            idle_rounds = 0
            _state["status"] = "running"

            for event_id in batch:
                await _process_one(event_id)
                save_state()
                await asyncio.sleep(PACE_SECONDS)
        except BaseException as outer:  # noqa: BLE001
            _state["status"] = "error_recovering"
            _state["last_error"] = (
                f"outer {type(outer).__name__}: {str(outer)[:200]}"
            )
            save_state()
            if isinstance(outer, (KeyboardInterrupt, SystemExit)):
                raise
            log.exception("outer loop error, sleeping 60s: %s", outer)
            await asyncio.sleep(60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        _state["status"] = "stopped"
        save_state()
