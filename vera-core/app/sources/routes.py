"""Dashboard CRUD for event sources + filter rules."""
import logging

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import desc, func, select

from vera_shared.db.engine import get_session
from vera_shared.db.models import Event, Source

from app.dashboard.auth import require_owner

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/sources")

_ALLOWED_TYPES = {"telegram", "gmail", "bank", "instagram", "generic"}


def _row(r: Source) -> dict:
    return {
        "id": r.id, "type": r.type, "name": r.name, "account": r.account,
        "enabled": r.enabled, "poll_interval_sec": r.poll_interval_sec,
        "base_threshold": r.base_threshold,
        "filters": r.filters or [],
        "config_keys": list((r.config or {}).keys()),
        "last_polled_at": r.last_polled_at.isoformat() if r.last_polled_at else None,
        "last_error": r.last_error, "intake_count": r.intake_count,
    }


@router.get("")
async def list_sources(_=Depends(require_owner)) -> list[dict]:
    async with get_session() as s:
        result = await s.execute(select(Source).order_by(Source.id))
        rows = result.scalars().all()
    return [_row(r) for r in rows]


@router.post("")
async def create_source(payload: dict = Body(...), _=Depends(require_owner)) -> dict:
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    src_type = (payload.get("type") or "").strip()
    if src_type not in _ALLOWED_TYPES:
        raise HTTPException(400, f"type must be one of {sorted(_ALLOWED_TYPES)}")
    async with get_session() as s:
        existing = await s.execute(select(Source).where(Source.name == name))
        if existing.scalar_one_or_none():
            raise HTTPException(409, f"source '{name}' already exists")
        row = Source(
            type=src_type,
            name=name,
            account=payload.get("account"),
            enabled=bool(payload.get("enabled", True)),
            poll_interval_sec=int(payload.get("poll_interval_sec", 120)),
            base_threshold=float(payload.get("base_threshold", 0.95)),
            filters=payload.get("filters") or [],
            config=payload.get("config") or {},
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
    return _row(row)


@router.put("/{source_id}")
async def update_source(source_id: int, payload: dict = Body(...),
                        _=Depends(require_owner)) -> dict:
    async with get_session() as s:
        row = await s.get(Source, source_id)
        if row is None:
            raise HTTPException(404, "source not found")
        for field in ("name", "account"):
            if field in payload:
                setattr(row, field, payload[field])
        if "enabled" in payload:
            row.enabled = bool(payload["enabled"])
        if "poll_interval_sec" in payload:
            row.poll_interval_sec = int(payload["poll_interval_sec"])
        if "base_threshold" in payload:
            row.base_threshold = float(payload["base_threshold"])
        if "filters" in payload:
            row.filters = payload["filters"] or []
        if "config" in payload:
            merged = dict(row.config or {})
            merged.update(payload["config"] or {})
            row.config = merged
        await s.commit()
        await s.refresh(row)
    return _row(row)


@router.delete("/{source_id}")
async def delete_source(source_id: int, _=Depends(require_owner)) -> dict:
    async with get_session() as s:
        row = await s.get(Source, source_id)
        if row is None:
            raise HTTPException(404, "source not found")
        await s.delete(row)
        await s.commit()
    return {"ok": True}


@router.get("/{source_id}/intake")
async def recent_intake(source_id: int, limit: int = 20,
                        _=Depends(require_owner)) -> dict:
    async with get_session() as s:
        row = await s.get(Source, source_id)
        if row is None:
            raise HTTPException(404, "source not found")
        result = await s.execute(
            select(Event).where(Event.account == row.name)
            .order_by(desc(Event.id)).limit(min(limit, 100))
        )
        events = result.scalars().all()
        total = await s.execute(
            select(func.count()).select_from(Event).where(Event.account == row.name)
        )
    return {
        "source": _row(row),
        "total_events": total.scalar() or 0,
        "recent": [
            {
                "id": e.id, "category": e.category,
                "preview": (e.content_text or "")[:160],
                "occurred_at": e.occurred_at.isoformat() if e.occurred_at else None,
                "triage_status": e.triage_status,
            }
            for e in events
        ],
    }
