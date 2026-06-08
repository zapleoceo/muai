"""POST /event/{source} endpoint — приём событий от ingestor'ов и webhook'ов.

Принимает RawEvent payload, делает dedup, сохраняет в DB, публикует
в Hatchet для обработки brain-triage.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from vera_shared.db.engine import get_session
from vera_shared.db.models import EventRow
from vera_shared.events.schema import RawEvent

from gateway.config import get_settings

log = logging.getLogger(__name__)
router = APIRouter()


def _check_internal_secret(provided: str | None) -> None:
    expected = get_settings().internal_secret
    if not expected:
        # Не падаем если не настроено — для dev mode
        return
    if not provided or provided != expected:
        raise HTTPException(401, "invalid internal secret")


@router.post("/event/{source}", status_code=201)
async def ingest_event(
    source: str,
    event: RawEvent,
    x_internal_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    """Приём события от ingestor сервиса.

    Path param `source` должен совпадать с event.source (защита от мисматча).
    """
    _check_internal_secret(x_internal_secret)

    if source.lower() != event.source.lower():
        raise HTTPException(
            400, f"Path source '{source}' != event.source '{event.source}'"
        )

    row = EventRow(
        source=event.source,
        source_event_id=event.source_event_id,
        account=event.account,
        category=event.category,
        content_text=event.content_text,
        content_extra=event.content_extra,
        entity_hints=[h.model_dump() for h in event.entity_hints],
        metadata_=event.metadata,
        occurred_at=event.occurred_at,
        triage_status="pending",
    )

    # Dedup explicit check (быстрее и понятнее чем catch IntegrityError)
    async with get_session() as s:
        existing = await s.execute(
            select(EventRow.id).where(
                EventRow.source == event.source,
                EventRow.source_event_id == event.source_event_id,
            )
        )
        existing_id = existing.scalar_one_or_none()
        if existing_id is not None:
            log.info("Dedup hit: %s/%s → event %s", event.source, event.source_event_id, existing_id)
            return {"ok": True, "event_id": existing_id, "deduped": True}

    try:
        async with get_session() as s:
            s.add(row)
            await s.flush()
            event_id = row.id
    except IntegrityError as exc:
        log.exception("Insert failed: %s", exc)
        raise HTTPException(500, f"insert failed: {exc}") from exc

    log.info("Event %s ingested: %s/%s", event_id, event.source, event.source_event_id)

    # TODO: publish to Hatchet `event.triage` workflow
    # await hatchet.publish("event.created", event_id=event_id)

    return {"ok": True, "event_id": event_id, "deduped": False}


@router.get("/api/events/{event_id}")
async def get_event(event_id: int) -> dict[str, Any]:
    async with get_session() as s:
        row = await s.get(EventRow, event_id)
        if row is None:
            raise HTTPException(404, "Event not found")
        return {
            "id": row.id,
            "source": row.source,
            "source_event_id": row.source_event_id,
            "account": row.account,
            "category": row.category,
            "content_text": row.content_text,
            "occurred_at": row.occurred_at.isoformat(),
            "received_at": row.received_at.isoformat() if row.received_at else None,
            "triage_status": row.triage_status,
            "triage_metadata": row.triage_metadata,
            "importance": row.importance,
        }
