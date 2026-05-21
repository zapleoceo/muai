import hmac
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from app.config import get_settings
from app.dashboard.auth import require_owner
from fastapi import Depends

from app.events.ingest import schedule_ingest
from app.events.store import list_recent, save_event

log = logging.getLogger(__name__)
router = APIRouter()


def _require_internal(secret_header: str | None) -> None:
    expected = get_settings().internal_secret
    if not secret_header or not hmac.compare_digest(secret_header, expected):
        raise HTTPException(401, "invalid X-Internal-Secret")


class EntityHint(BaseModel):
    type: str
    identifier: str | None = None
    name: str | None = None
    extra: dict[str, Any] | None = None


class EventPayload(BaseModel):
    source: str
    source_event_id: str | None = None
    account: str | None = None
    category: str = "generic"
    content_text: str | None = None
    content_extra: dict[str, Any] | None = None
    entity_hints: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] | None = None
    occurred_at: datetime | None = None


@router.post("/event")
async def ingest_event(
    payload: EventPayload,
    x_internal_secret: str | None = Header(default=None),
) -> dict:
    _require_internal(x_internal_secret)
    occurred = payload.occurred_at or datetime.utcnow()
    event, is_new = await save_event(
        source=payload.source,
        source_event_id=payload.source_event_id,
        account=payload.account,
        category=payload.category,
        content_text=payload.content_text,
        content_extra=payload.content_extra,
        entity_hints=payload.entity_hints,
        metadata=payload.metadata,
        occurred_at=occurred,
    )
    if not is_new:
        return {"ok": True, "event_id": event.id, "deduped": True}
    schedule_ingest(
        event.id,
        source=event.source,
        category=event.category,
        content_text=event.content_text,
        entity_hints=event.entity_hints,
        metadata=event.metadata_,
        occurred_at=event.occurred_at,
    )
    return {"ok": True, "event_id": event.id}


@router.get("/api/events")
async def api_events(_=Depends(require_owner), limit: int = 50,
                     source: str | None = None) -> list[dict]:
    events = await list_recent(limit=limit, source=source)
    return [
        {
            "id": e.id, "source": e.source, "source_event_id": e.source_event_id,
            "account": e.account, "category": e.category,
            "content_text": (e.content_text or "")[:1000],
            "entity_hints": e.entity_hints or [],
            "metadata": e.metadata_ or {},
            "occurred_at": e.occurred_at.isoformat() if e.occurred_at else None,
            "received_at": e.received_at.isoformat() if e.received_at else None,
            "graphiti_episode_uuid": e.graphiti_episode_uuid,
            "triage_status": e.triage_status,
        }
        for e in events
    ]
