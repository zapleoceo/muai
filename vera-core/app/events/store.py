import logging
from datetime import datetime

from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models import Event

log = logging.getLogger(__name__)


async def save_event(
    *, source: str, source_event_id: str | None, account: str | None,
    category: str, content_text: str | None, content_extra: dict | None,
    entity_hints: list | None, metadata: dict | None,
    occurred_at: datetime,
) -> tuple[Event, bool]:
    """Returns (event, is_new). When (source, source_event_id) already
    exists, returns the existing row with is_new=False so the caller
    can skip re-scheduling ingest/triage."""
    async with get_session() as session:
        if source_event_id:
            existing = await session.execute(
                select(Event).where(
                    Event.source == source,
                    Event.source_event_id == source_event_id,
                )
            )
            row = existing.scalars().first()
            if row is not None:
                return row, False
        e = Event(
            source=source, source_event_id=source_event_id, account=account,
            category=category, content_text=content_text, content_extra=content_extra,
            entity_hints=entity_hints, metadata_=metadata,
            occurred_at=occurred_at,
        )
        session.add(e)
        await session.commit()
        await session.refresh(e)
        return e, True


async def mark_episode(event_id: int, episode_uuid: str | None) -> None:
    async with get_session() as session:
        e = await session.get(Event, event_id)
        if e:
            e.graphiti_episode_uuid = episode_uuid
            await session.commit()


async def list_recent(limit: int = 50, source: str | None = None) -> list[Event]:
    async with get_session() as session:
        q = select(Event).order_by(Event.id.desc()).limit(limit)
        if source:
            q = q.where(Event.source == source)
        result = await session.execute(q)
        return list(result.scalars().all())
