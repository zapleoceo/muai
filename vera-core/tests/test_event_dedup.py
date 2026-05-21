"""Once an event with the same source_event_id has been ingested, a second
POST /event for the same key must NOT re-schedule triage."""
from datetime import datetime

import pytest

from app.events.store import save_event


@pytest.mark.asyncio
async def test_save_event_returns_existing_with_is_new_false():
    occurred = datetime.utcnow()
    e1, new1 = await save_event(
        source="gmail", source_event_id="dedup-test-1", account="a@b.c",
        category="communication", content_text="hello",
        content_extra=None, entity_hints=[], metadata=None,
        occurred_at=occurred,
    )
    assert new1 is True
    e2, new2 = await save_event(
        source="gmail", source_event_id="dedup-test-1", account="a@b.c",
        category="communication", content_text="hello again — same id",
        content_extra=None, entity_hints=[], metadata=None,
        occurred_at=occurred,
    )
    assert new2 is False
    assert e2.id == e1.id


@pytest.mark.asyncio
async def test_save_event_distinct_ids_are_new():
    occurred = datetime.utcnow()
    e1, new1 = await save_event(
        source="gmail", source_event_id="dedup-test-A", account=None,
        category="x", content_text="a", content_extra=None,
        entity_hints=[], metadata=None, occurred_at=occurred,
    )
    e2, new2 = await save_event(
        source="gmail", source_event_id="dedup-test-B", account=None,
        category="x", content_text="b", content_extra=None,
        entity_hints=[], metadata=None, occurred_at=occurred,
    )
    assert new1 and new2 and e1.id != e2.id
