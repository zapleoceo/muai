"""Приём события от ingestor'а и его выдача наружу — без HTTP."""
from __future__ import annotations

import logging
from typing import Any

from vera_shared.events.schema import RawEvent
from vera_shared.redact_view import redact_tree, redact_view

from gateway.events_repo import get_event_row, insert_event_once

log = logging.getLogger(__name__)


async def ingest_raw_event(event: RawEvent) -> dict[str, Any]:
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
    event_id, deduped = await insert_event_once(values)
    if deduped:
        log.info("Dedup hit: %s/%s → event %s",
                 event.source, event.source_event_id, event_id)
    else:
        log.info("Event %s ingested: %s/%s",
                 event_id, event.source, event.source_event_id)
    return {"ok": True, "event_id": event_id, "deduped": deduped}


async def get_event_view(event_id: int) -> dict[str, Any] | None:
    """Событие для внутренних клиентов: текст и метаданные — через маскировку."""
    row = await get_event_row(event_id)
    if row is None:
        return None
    return {
        "id": row.id,
        "source": row.source,
        "source_event_id": row.source_event_id,
        "account": row.account,
        "category": row.category,
        "content_text": redact_view(row.content_text) if row.content_text else row.content_text,
        "occurred_at": row.occurred_at.isoformat(),
        "received_at": row.received_at.isoformat() if row.received_at else None,
        "triage_status": row.triage_status,
        "triage_metadata": redact_tree(row.triage_metadata),
        "importance": row.importance,
        "nature": row.nature,
        "project": row.project,
        "ready_subtype": row.ready_subtype,
    }
