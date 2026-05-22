"""Tests for the A/B/C bundle: replay table, self-tools, stale event expiry."""
from datetime import datetime

import pytest
from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models import DecisionReplay, Event
from app.events.store import save_event
from app.triage import replay as rp
from app.triage.dispatcher import _FINAL_STATUSES


@pytest.mark.asyncio
async def test_replay_record_then_suggest_returns_most_recent():
    occurred = datetime.utcnow()
    ev, _ = await save_event(
        source="gmail", source_event_id="replay-test-1", account="a@b.c",
        category="x", content_text="hello", content_extra=None,
        entity_hints=[{"type": "person", "identifier": "boss@x.com"}],
        metadata=None, occurred_at=occurred,
    )
    await rp.record(ev, "Заархивировать", "gmail_modify_thread",
                     {"action": "archive"})
    sugg = await rp.suggest(ev)
    assert len(sugg) >= 1
    top = sugg[0]
    assert top["tool"] == "gmail_modify_thread"
    assert top["args"]["action"] == "archive"
    assert top["count"] == 1


@pytest.mark.asyncio
async def test_replay_count_increments_on_repeat():
    occurred = datetime.utcnow()
    ev, _ = await save_event(
        source="gmail", source_event_id="replay-test-2", account="a@b.c",
        category="x", content_text="ping",
        content_extra=None,
        entity_hints=[{"type": "person", "identifier": "repeat@x.com"}],
        metadata=None, occurred_at=occurred,
    )
    for _i in range(3):
        await rp.record(ev, "Игнорировать", None, None)
    sugg = await rp.suggest(ev)
    assert sugg[0]["count"] == 3


@pytest.mark.asyncio
async def test_replay_no_sender_no_record():
    occurred = datetime.utcnow()
    ev, _ = await save_event(
        source="gmail", source_event_id="replay-test-3", account=None,
        category="x", content_text="no sender",
        content_extra=None, entity_hints=[], metadata=None,
        occurred_at=occurred,
    )
    await rp.record(ev, "x", None, None)
    sugg = await rp.suggest(ev)
    assert sugg == []


def test_expired_in_final_statuses():
    assert "expired" in _FINAL_STATUSES


def test_self_tool_specs_have_required_shape():
    from app.system.routes import TOOL_SPECS
    names = {t["name"] for t in TOOL_SPECS}
    assert "system_deploy" in names
    assert "system_status" in names
    for spec in TOOL_SPECS:
        assert isinstance(spec["params"], list)
        assert spec["description"]
