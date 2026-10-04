"""One transaction for the legacy Instagram projection and its shadow evidence."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection
from vera_shared.db.models import EventRow
from vera_shared.events.schema import RawEvent
from vera_shared.ingest.shadow_instagram import ingest_instagram_shadow
from vera_shared.ingest.shadow_types import Quarantine


async def ingest_shadow_event(
    conn: AsyncConnection,
    event: RawEvent,
    values: dict[str, Any],
    *,
    phase_hook: Callable[[str], None] | None = None,
) -> tuple[int, bool]:
    """Atomically ingest, including an immutable snapshot of a preexisting legacy edit."""
    if conn.in_transaction():
        raise Quarantine("gateway shadow ingest requires idle connection")
    async with conn.begin():
        if (event.metadata or {}).get("deleted") is True:
            values = values | {"content_text": ""}
        inserted = await conn.execute(
            pg_insert(EventRow)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["source", "source_event_id"])
            .returning(EventRow.id)
        )
        event_id = inserted.scalar_one_or_none()
        deduped = event_id is None
        previous = None
        if deduped:
            previous = (
                await conn.execute(
                    select(
                        EventRow.id,
                        EventRow.account,
                        EventRow.content_text,
                        EventRow.metadata_,
                        EventRow.occurred_at,
                    )
                    .where(
                        EventRow.source == event.source,
                        EventRow.source_event_id == event.source_event_id,
                    )
                    .with_for_update()
                )
            ).one_or_none()
            if previous is None:
                raise Quarantine("legacy event identity unavailable")
            if previous.account != event.account:
                raise Quarantine("legacy event belongs to another account")
            event_id = previous.id
        assert event_id is not None
        if phase_hook:
            phase_hook("legacy")

        if previous is not None and (
            previous.content_text != event.content_text
            or previous.metadata_ != event.metadata
        ):
            head = (
                await conn.execute(
                    text(
                        "SELECT source_head_revision FROM brain_source_objects "
                        "WHERE provider='instagram' AND account_id=:account "
                        "AND object_type='message' AND external_id=:external_id"
                    ),
                    {"account": event.account, "external_id": event.source_event_id},
                )
            ).scalar_one_or_none()
            if head is None:
                if (event.metadata or {}).get("source_revision") != 2:
                    raise Quarantine("legacy edit needs authoritative source revision 2")
                original_meta = dict(previous.metadata_ or {})
                original_meta.update(
                    thread_id=(event.metadata or {}).get("thread_id"),
                    message_id=(event.metadata or {}).get("message_id"),
                    source_revision=1,
                )
                original = event.model_copy(
                    update={
                        "content_text": previous.content_text,
                        "metadata": original_meta,
                        "occurred_at": previous.occurred_at or event.occurred_at,
                    }
                )
                await ingest_instagram_shadow(
                    conn, original, legacy_event_id=event_id, in_transaction=True
                )

        await ingest_instagram_shadow(
            conn, event, legacy_event_id=event_id, in_transaction=True
        )
        if phase_hook:
            phase_hook("shadow")
        if previous is not None:
            deleted = (event.metadata or {}).get("deleted") is True
            target = "" if deleted else event.content_text
            if previous.content_text != target or previous.metadata_ != event.metadata:
                await conn.execute(
                    update(EventRow)
                    .where(EventRow.id == event_id, EventRow.account == event.account)
                    .values(
                        content_text=target,
                        metadata_=event.metadata,
                        triage_status="done" if deleted else "pending",
                    )
                )
        if phase_hook:
            phase_hook("projection")
        return event_id, deduped
