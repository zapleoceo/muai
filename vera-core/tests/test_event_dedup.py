"""Once an event with the same source_event_id has been ingested, a second
POST /event for the same key must NOT re-schedule triage. Plus _run_triage
self-guard: even if someone re-calls it on a decided event, it bails."""
from datetime import datetime

import pytest
from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models import Event
from app.events.store import save_event
from app.triage.dispatcher import _FINAL_STATUSES, _run_triage


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


def test_final_statuses_cover_expected():
    expected = {"decided", "executed", "execute_failed", "awaiting_user",
                "auto_executed", "auto_failed", "failed", "proposal_only"}
    assert expected <= _FINAL_STATUSES


@pytest.mark.asyncio
async def test_run_triage_skips_decided_event():
    """If event status is already final, _run_triage must NOT call out
    to LLM, MCP, or send_card. We use the side-effect that an LLM-less
    triage would crash on the fixture's env — early-return must prevent
    that."""
    occurred = datetime.utcnow()
    ev, _ = await save_event(
        source="gmail", source_event_id="guard-test-1", account=None,
        category="x", content_text="some text", content_extra=None,
        entity_hints=[], metadata=None, occurred_at=occurred,
    )
    async with get_session() as s:
        row = await s.get(Event, ev.id)
        row.triage_status = "decided"
        row.triage_result = {"summary": "already handled"}
        await s.commit()
    # Must return without raising, without calling LLM:
    await _run_triage(ev.id)
    async with get_session() as s:
        row = await s.get(Event, ev.id)
        assert row.triage_status == "decided"  # untouched


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
