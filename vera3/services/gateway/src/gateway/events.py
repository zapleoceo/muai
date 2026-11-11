"""POST /event/{source} — приём событий от ingestor'ов и webhook'ов;
GET /api/events/{id} — событие по id. Логика в `events_service`, SQL в
`events_repo`."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException
from vera_shared.events.schema import RawEvent

from gateway.auth import check_internal_secret
from gateway.events_service import get_event_view, ingest_raw_event

router = APIRouter()


@router.post("/event/{source}", status_code=201)
async def ingest_event(
    source: str,
    event: RawEvent,
    x_internal_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    """Path param `source` должен совпадать с event.source (защита от мисматча)."""
    check_internal_secret(x_internal_secret)
    if source.lower() != event.source.lower():
        raise HTTPException(
            400, f"Path source '{source}' != event.source '{event.source}'"
        )
    return await ingest_raw_event(event)


@router.get("/api/events/{event_id}")
async def get_event(
    event_id: int,
    x_internal_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    # Had NO auth check at all (unlike every other route in this file) —
    # returns full content_text (email/message body) by event_id. Not
    # currently internet-reachable (nginx only proxies /event/, /v1/,
    # /webhook/ to gateway — /api/ falls through to dashboard), but that's
    # an accident of nginx config, not a guarantee this code should rely on.
    check_internal_secret(x_internal_secret)
    view = await get_event_view(event_id)
    if view is None:
        raise HTTPException(404, "Event not found")
    return view
