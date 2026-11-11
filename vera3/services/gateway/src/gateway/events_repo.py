"""SQL для `/event/{source}` и `/api/events/{id}`.

Дедупликация через ON CONFLICT DO NOTHING (UNIQUE `uq_event_source_id` в
db/models.py) — это закрывает гонку check-and-insert между параллельными
backfill и poller.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from vera_shared.db.engine import get_session
from vera_shared.db.models import EventRow


async def insert_event_once(values: dict[str, Any]) -> tuple[int | None, bool]:
    """(id события, был ли это дедуп). При конфликте RETURNING пуст —
    тогда подбираем id уже существующей строки."""
    async with get_session() as s:
        stmt = (
            pg_insert(EventRow)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["source", "source_event_id"])
            .returning(EventRow.id)
        )
        event_id = (await s.execute(stmt)).scalar_one_or_none()
        if event_id is not None:
            return event_id, False
        existing = await s.execute(
            select(EventRow.id).where(
                EventRow.source == values["source"],
                EventRow.source_event_id == values["source_event_id"],
            )
        )
        return existing.scalar_one_or_none(), True


async def get_event_row(event_id: int) -> EventRow | None:
    async with get_session() as s:
        return await s.get(EventRow, event_id)
