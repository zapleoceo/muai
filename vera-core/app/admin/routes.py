import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func

from vera_shared.db.engine import get_session
from vera_shared.db.models import Event, Token
from vera_shared.tokens import repository as token_repo

from app.dashboard.auth import require_owner
from app.triage.dispatcher import schedule_triage

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin")


@router.get("/brain-backfill")
async def brain_backfill_progress(_=Depends(require_owner)) -> dict:
    """Read the systemd backfill job's progress file. Maps the script's
    new schema (done/errors/last_ok_at/...) to the legacy field names the
    dashboard expects (ok/err/last_update/...) so the widget keeps working
    without a frontend change.
    """
    progress_file = Path("/data/backfill_progress.json")
    state: dict = {}
    if progress_file.exists():
        try:
            state = json.loads(progress_file.read_text())
        except Exception as exc:
            log.warning("backfill state read failed: %s", exc)

    # New script writes: total, done, errors, skipped, last_event_id,
    # last_ok_at, last_error, rate_per_min, status. Map to legacy keys.
    ok = int(state.get("done") or state.get("ok") or 0)
    err = int(state.get("errors") or state.get("err") or 0)
    processed = ok + err
    candidates = int(
        state.get("total") or state.get("candidates_filtered") or 0
    )
    # Frontend appends "Z" before new Date() — strip any trailing Z/tz
    # info from our ISO so the result stays a single valid UTC stamp.
    last_update_iso = state.get("last_ok_at") or state.get("last_update")
    if last_update_iso and isinstance(last_update_iso, str):
        last_update_iso = last_update_iso.rstrip("Z").split("+")[0]
    running_flag = (
        state.get("status") == "running"
        if "status" in state
        else bool(state.get("running"))
    )

    # Live brain count from DB
    async with get_session() as s:
        brain_total = (await s.execute(
            select(func.count(Event.id))
            .where(Event.graphiti_episode_uuid.is_not(None))
        )).scalar() or 0
        missing = (await s.execute(
            select(func.count(Event.id))
            .where(Event.graphiti_episode_uuid.is_(None))
            .where(Event.occurred_at >= "2026-05-03")
        )).scalar() or 0

    # ETA: prefer the rolling rate the script publishes (events/min),
    # fall back to averaged-since-start.
    eta_min = None
    rate_per_min = float(state.get("rate_per_min") or 0)
    remaining = max(0, candidates - ok)
    if rate_per_min > 0 and remaining > 0:
        eta_min = round(remaining / rate_per_min)
    elif state.get("started_at") and ok > 0:
        try:
            started_str = str(state["started_at"]).replace("Z", "+00:00")
            started = datetime.fromisoformat(started_str)
            now = datetime.utcnow().replace(tzinfo=started.tzinfo) if started.tzinfo else datetime.utcnow()
            elapsed_s = (now - started).total_seconds()
            if elapsed_s > 0:
                eta_min = round(remaining * elapsed_s / ok / 60)
        except Exception:
            pass

    return {
        "brain_total_episodes": brain_total,
        "events_missing_last_month": missing,
        "started_at": state.get("started_at"),
        "last_update": last_update_iso,
        "candidates_total": candidates,
        "candidates_filtered": candidates,
        "processed": processed,
        "ok": ok,
        "err": err,
        "current_event_id": state.get("last_event_id"),
        "current_event_time": None,
        "last_ok_event_id": state.get("last_event_id"),
        "last_err_msg": state.get("last_error") or state.get("last_err_msg"),
        "eta_minutes": eta_min,
        "running": running_flag,
        "rate_per_min": rate_per_min,
    }


@router.get("/tokens")
async def list_tokens(_=Depends(require_owner)) -> list[dict]:
    """List all tokens with their per-key limits + today's usage."""
    async with get_session() as s:
        rows = (await s.execute(
            select(Token).order_by(Token.provider, Token.id)
        )).scalars().all()
    return [
        {
            "id": r.id, "provider": r.provider, "label": r.label,
            "is_active": r.is_active,
            "daily_limit": r.daily_limit,
            "daily_used": r.daily_used,
            "daily_cost_limit_usd": r.daily_cost_limit_usd,
            "daily_cost_used_usd": round(r.daily_cost_used_usd or 0.0, 4),
            "cost_usd_total": round(r.cost_usd or 0.0, 4),
            "cooldown_until": (r.cooldown_until.isoformat()
                                if r.cooldown_until else None),
            "error_count": r.error_count,
        }
        for r in rows
    ]


@router.post("/tokens/{token_id}/cost-limit")
async def set_cost_limit(token_id: int, payload: dict,
                          _=Depends(require_owner)) -> dict:
    """Set per-key daily cost cap. POST {limit_usd: 2.0} or {limit_usd: null}."""
    raw = payload.get("limit_usd")
    if raw is None:
        limit: float | None = None
    else:
        try:
            limit = float(raw)
            if limit < 0:
                raise HTTPException(400, "limit_usd must be >= 0")
        except (TypeError, ValueError):
            raise HTTPException(400, "limit_usd must be a number or null")
    ok = await token_repo.set_cost_limit(token_id, limit)
    if not ok:
        raise HTTPException(404, "token not found")
    return {"ok": True, "token_id": token_id, "daily_cost_limit_usd": limit}


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
