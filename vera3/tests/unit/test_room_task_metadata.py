"""Metadata corrections stay outside lease, progress, and owner hold state."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from vera_mcp import room_tools as r
from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskRow
from vera_shared.room.tasks import TaskBusy

pytestmark = pytest.mark.asyncio


def ctx(actor: str) -> SimpleNamespace:
    return SimpleNamespace(request_context=SimpleNamespace(
        request=SimpleNamespace(scope={"mcp_client": actor})))


DOT, CLAUDE = ctx("dot"), ctx("claude")


async def test_disabled_by_default(sqlite_db, monkeypatch):
    monkeypatch.delenv("ROOM_METADATA_ENABLED", raising=False)
    opened = (await r.room_task_open("T1", CLAUDE))["task"]
    with pytest.raises(ValueError, match="disabled"):
        await r.room_task_metadata_correct("T1", opened["updated_at"], "routing fix", DOT,
                                           responsible="dot")
    assert [e["kind"] for e in (await r.room_task_history("T1"))["events"]] == ["created"]


async def test_blocked_task_metadata_correction_is_audited_without_claim_or_progress(
        sqlite_db, monkeypatch):
    monkeypatch.setenv("ROOM_METADATA_ENABLED", "1")
    await r.room_task_open("T1", CLAUDE, next_action="old")
    await r.room_task_claim("T1", CLAUDE)
    before = (await r.room_task_release("T1", 1, CLAUDE, status="blocked"))["task"]
    result = await r.room_task_metadata_correct(
        "T1", before["updated_at"], "correct coordinator assignment", DOT,
        responsible="dot", next_action="wait for evidence")
    after = result["task"]
    assert result["changed"] is True
    for key in ("status", "lease_holder", "lease_until", "fencing_token", "owner",
                "last_progress_at", "last_progress_text", "waiting_until", "waiting_reason"):
        assert after[key] == before[key], key
    assert after["status"] == "blocked"
    assert (after["responsible"], after["next_action"]) == ("dot", "wait for evidence")
    events = (await r.room_task_history("T1"))["events"]
    assert [e["kind"] for e in events] == ["created", "claimed", "released",
                                             "metadata_updated"]
    assert events[-1]["agent"] == "dot"
    assert events[-1]["data"] == {
        "before": {"responsible": None, "next_action": "old"},
        "after": {"responsible": "dot", "next_action": "wait for evidence"}}
    again = await r.room_task_metadata_correct(
        "T1", before["updated_at"], "duplicate", DOT,
        responsible="dot", next_action="wait for evidence")
    assert again["changed"] is False
    assert len((await r.room_task_history("T1"))["events"]) == len(events)
    with pytest.raises(TaskBusy, match="changed since"):
        await r.room_task_metadata_correct("T1", before["updated_at"], "stale", DOT,
                                           responsible="someone else")


async def test_rejects_other_actor_live_lease_and_owner_hold(sqlite_db, monkeypatch):
    monkeypatch.setenv("ROOM_METADATA_ENABLED", "1")
    opened = (await r.room_task_open("T1", CLAUDE))["task"]
    stamp = opened["updated_at"]
    with pytest.raises(TaskBusy, match="dot actor"):
        await r.room_task_metadata_correct("T1", stamp, "reason", CLAUDE,
                                           responsible="claude")
    await r.room_task_claim("T1", CLAUDE)
    with pytest.raises(TaskBusy, match="not eligible"):
        await r.room_task_metadata_correct("T1", stamp, "reason", DOT, responsible="dot")
    await r.room_task_release("T1", 1, CLAUDE, status="blocked")
    async with get_session() as s:
        row = await s.get(RoomTaskRow, ("main", "T1"))
        row.owner = "owner"
    with pytest.raises(TaskBusy, match="not eligible"):
        await r.room_task_metadata_correct("T1", stamp, "reason", DOT, responsible="dot")
    assert "metadata_updated" not in [e["kind"] for e in
                                      (await r.room_task_history("T1"))["events"]]


@pytest.mark.parametrize("hold", ["paused", "review"])
async def test_rejects_paused_and_review_holds(sqlite_db, monkeypatch, hold):
    monkeypatch.setenv("ROOM_METADATA_ENABLED", "1")
    await r.room_task_claim("T1", CLAUDE)
    await r.room_task_state("T1", 1, CLAUDE, hold, "owner hold")
    blocked = (await r.room_task_release("T1", 1, CLAUDE, status="blocked"))["task"]
    with pytest.raises(TaskBusy, match="owner review"):
        await r.room_task_metadata_correct("T1", blocked["updated_at"], "routing", DOT,
                                           next_action="wait")
