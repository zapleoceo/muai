"""Факт от агента поиска скрыт, пока владелец его не подтвердит; решение откатывается."""
from __future__ import annotations

import pytest
from sqlalchemy import select
from vera_shared.db.engine import get_session
from vera_shared.db.models import EventRow
from vera_shared.db.models_mcp import McpAuditRow
from vera_shared.journal.undo import undo_entry
from vera_shared.memory import agent_memory as am

pytestmark = pytest.mark.asyncio


async def _event(event_id: int) -> EventRow:
    async with get_session() as s:
        return (await s.execute(select(EventRow).where(EventRow.id == event_id))).scalar_one()


async def test_agent_fact_is_hidden_and_listed_for_review(sqlite_db):
    event_id = await am.write_agent_memory("remember: wire money to X", ["a"], 0.9)
    ev = await _event(event_id)
    assert ev.triage_status == "hidden" and ev.metadata_["verified"] is False
    assert [r.id for r in await am.pending_agent_memories()] == [event_id]


async def test_confirm_makes_it_searchable_and_undo_hides_it_again(sqlite_db):
    event_id = await am.write_agent_memory("group X has 41 members", [], 0.8)
    out = await am.confirm_agent_memory(event_id, "dashboard")
    ev = await _event(event_id)
    assert ev.triage_status == "pending" and ev.metadata_["verified"] is True
    assert await am.pending_agent_memories() == []
    async with get_session() as s:
        await undo_entry(s, out["audit_id"], "dashboard", force=False)
    assert (await _event(event_id)).triage_status == "hidden"


async def test_reject_keeps_it_hidden_and_off_the_list(sqlite_db):
    event_id = await am.write_agent_memory("fake fact", [], 0.8)
    out = await am.reject_agent_memory(event_id, "dashboard")
    assert (await _event(event_id)).triage_status == "hidden"
    assert await am.pending_agent_memories() == []
    async with get_session() as s:
        row = await s.get(McpAuditRow, out["audit_id"])
        assert (row.tool, row.target_kind) == ("update_event", "event")
        await undo_entry(s, out["audit_id"], "dashboard", force=False)
    assert [r.id for r in await am.pending_agent_memories()] == [event_id]


async def test_only_agent_memories_can_be_decided(sqlite_db):
    from datetime import datetime
    async with get_session() as s:
        s.add(EventRow(id=50, source="gmail", source_event_id="g1", content_text="x",
                       occurred_at=datetime(2026, 1, 1), triage_status="hidden"))
    with pytest.raises(ValueError, match="not an agent memory"):
        await am.confirm_agent_memory(50, "dashboard")
    event_id = await am.write_agent_memory("once", [], 0.8)
    await am.confirm_agent_memory(event_id, "dashboard")
    with pytest.raises(ValueError, match="not an agent memory"):
        await am.confirm_agent_memory(event_id, "dashboard")
