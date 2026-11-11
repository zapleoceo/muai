"""SQL очереди `claude_session_queue` и события-выжимки сессии."""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from vera_shared.db.engine import get_session
from vera_shared.db.models import ClaudeSessionQueueRow, EventRow


async def enqueue_session(session_id: str, incoming: dict[Any, Any],
                          turns: int) -> str | None:
    """Ставит сессию в очередь; None — уже осмыслена в этом объёме."""
    async with get_session() as s:
        stmt = (
            pg_insert(ClaudeSessionQueueRow)
            .values({ClaudeSessionQueueRow.session_id: session_id, **incoming})
            .on_conflict_do_update(
                index_elements=["session_id"],
                set_=incoming,
                # Осмысленное не переосмысляем: в очередь возвращаем только
                # сессию, которую дописали.
                where=ClaudeSessionQueueRow.done_turns < turns,
            )
            .returning(ClaudeSessionQueueRow.session_id)
        )
        return (await s.execute(stmt)).scalar_one_or_none()


async def get_queued_session(session_id: str) -> ClaudeSessionQueueRow | None:
    async with get_session() as s:
        return (await s.execute(
            select(ClaudeSessionQueueRow)
            .where(ClaudeSessionQueueRow.session_id == session_id)
        )).scalar_one_or_none()


async def upsert_session_event(identity: dict[Any, Any], fresh: dict[Any, Any],
                               turn_count: int) -> int | None:
    async with get_session() as s:
        stmt = (
            pg_insert(EventRow)
            .values({**identity, **fresh})
            .on_conflict_do_update(
                index_elements=["source", "source_event_id"],
                set_=fresh,
                # Реплик не прибавилось — не трогаем событие и его embedding.
                # Исключение — пустышка прошлого захода (distilled=false):
                # настоящая выжимка обязана её перезаписать.
                where=(func.coalesce(
                    EventRow.metadata_["turns"].as_integer(), 0) < turn_count)
                | (func.coalesce(
                    EventRow.metadata_["distilled"].as_boolean(), False).is_(False)),
            )
            .returning(EventRow.id)
        )
        return (await s.execute(stmt)).scalar_one_or_none()
