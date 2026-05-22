"""Sanity tests for the upcoming triggers DB schema (S1 prep)."""
import pytest

from sqlalchemy import inspect, text

from vera_shared.db.engine import get_engine


@pytest.mark.asyncio
async def test_core_tables_exist():
    eng = get_engine()
    async with eng.connect() as c:
        names = await c.run_sync(lambda sync: inspect(sync).get_table_names())
    assert "tokens" in names
    assert "agents" in names
    assert "events" in names
    assert "gmail_accounts" in names


@pytest.mark.asyncio
async def test_events_idempotent_on_source_event_id():
    """save_event is the dedupe point for incoming events."""
    from datetime import datetime
    from app.events.store import save_event
    e1, new1 = await save_event(
        source="test", source_event_id="dup-1", account=None, category="x",
        content_text="hello", content_extra=None, entity_hints=None,
        metadata=None, occurred_at=datetime.utcnow(),
    )
    e2, new2 = await save_event(
        source="test", source_event_id="dup-1", account=None, category="x",
        content_text="hello again", content_extra=None, entity_hints=None,
        metadata=None, occurred_at=datetime.utcnow(),
    )
    assert e1.id == e2.id
    assert new1 is True
    assert new2 is False
