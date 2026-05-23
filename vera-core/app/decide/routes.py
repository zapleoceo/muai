"""Shadow API for v3 decide — queryable without affecting v2 triage."""
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException

from vera_shared.db.engine import get_session
from vera_shared.db.models import Event

from app.dashboard.auth import require_owner
from app.decide.dispatch import decide
from app.decide.explain import explain

router = APIRouter(prefix="/api/decide")


@router.get("/{event_id}")
async def decide_for_event(event_id: int, _=Depends(require_owner)) -> dict:
    async with get_session() as s:
        ev = await s.get(Event, event_id)
    if ev is None:
        raise HTTPException(404, "event not found")
    hints = ev.entity_hints or []
    d = await decide(hints)
    return {
        "event_id": event_id,
        "band": d.band,
        "chosen": _scored_dict(d.chosen),
        "candidates": [_scored_dict(c) for c in d.candidates],
        "reason": d.reason,
        "explanation": explain(d),
    }


def _scored_dict(s) -> dict | None:
    if s is None:
        return None
    return {
        "label": s.candidate.label,
        "tool": s.candidate.tool,
        "args": s.candidate.args,
        "score": round(s.score, 3),
        "breakdown": s.breakdown,
        "blocked_by": s.blocked_by,
    }
