"""POST /event/{source} endpoint — приём событий от ingestor'ов и webhook'ов.

Дедупликация через ON CONFLICT DO NOTHING (UNIQUE constraint
`uq_event_source_id` в db/models.py). Это закрывает race condition
между check-and-insert при concurrent backfill + poller.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

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

    values = {
        "source": event.source,
        "source_event_id": event.source_event_id,
        "account": event.account,
        "category": event.category,
        "content_text": event.content_text,
        "content_extra": event.content_extra,
        "entity_hints": [h.model_dump() for h in event.entity_hints],
        "metadata_": event.metadata,
        "occurred_at": event.occurred_at,
        "triage_status": "pending",
    }

    # INSERT ... ON CONFLICT (source, source_event_id) DO NOTHING RETURNING id.
    # Если конфликт — RETURNING пуст, делаем SELECT уже существующего ID.
    async with get_session() as s:
        stmt = (
            pg_insert(EventRow)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["source", "source_event_id"])
            .returning(EventRow.id)
        )
        result = await s.execute(stmt)
        event_id = result.scalar_one_or_none()

        if event_id is None:
            # Дедуп hit — событие уже было. Подберём существующий id.
            existing = await s.execute(
                select(EventRow.id).where(
                    EventRow.source == event.source,
                    EventRow.source_event_id == event.source_event_id,
                )
            )
            event_id = existing.scalar_one_or_none()
            log.info("Dedup hit: %s/%s → event %s",
                     event.source, event.source_event_id, event_id)
            return {"ok": True, "event_id": event_id, "deduped": True}

    log.info("Event %s ingested: %s/%s", event_id, event.source, event.source_event_id)
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
