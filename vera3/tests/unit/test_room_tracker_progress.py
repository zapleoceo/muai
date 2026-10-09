"""Трекер задач, шаг 2: progress ≠ heartbeat, состояния, refs, история."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from vera_mcp import room_tools as r
from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskRow
from vera_shared.room import task_progress, tasks
from vera_shared.room.tasks import StaleLease

pytestmark = pytest.mark.asyncio


def ctx(client: str):
    return SimpleNamespace(request_context=SimpleNamespace(
        request=SimpleNamespace(scope={"mcp_client": client})))


CLAUDE, CODEX = ctx("claude"), ctx("codex")


async def expire_lease(task_id: str, room: str = "main") -> None:
    async with get_session() as s:
        row = await s.get(RoomTaskRow, (room, task_id))
        row.lease_until = row.lease_until - timedelta(hours=10)


async def kinds(task_id: str = "T1") -> list[str]:
    return [e["kind"] for e in (await r.room_task_history(task_id))["events"]]


async def test_progress_sets_fields_and_heartbeat_does_not(sqlite_db):
    await r.room_task_claim("T1", CLAUDE, session="laptop", account="a1")
    beat = (await r.room_task_update("T1", 1, CLAUDE, extend_seconds=600))["task"]
    assert beat["last_progress_at"] is None
    done = (await r.room_task_progress("T1", 1, CLAUDE, "migrated 3 tables",
                                       next_checkpoint_seconds=300))["task"]
    assert done["last_progress_text"] == "migrated 3 tables"
    assert done["last_progress_at"] and done["next_checkpoint_at"]
    assert (done["holder_session"], done["holder_account"]) == ("laptop", "a1")
    again = (await r.room_task_update("T1", 1, CLAUDE, extend_seconds=600))["task"]
    assert again["last_progress_at"] == done["last_progress_at"]
    assert await kinds() == ["created", "claimed", "heartbeat", "progress", "heartbeat"]


async def test_progress_requires_live_lease_and_current_token(sqlite_db):
    await r.room_task_claim("T1", CLAUDE)
    with pytest.raises(StaleLease):
        await r.room_task_progress("T1", 1, CODEX, "mine now")
    with pytest.raises(StaleLease):
        await r.room_task_progress("T1", 2, CLAUDE, "wrong token")
    await expire_lease("T1")
    with pytest.raises(StaleLease):
        await r.room_task_progress("T1", 1, CLAUDE, "too late")
    assert "progress" not in await kinds()


@pytest.mark.parametrize("seconds", [59, 86_401])
async def test_checkpoint_bounds(sqlite_db, seconds):
    await r.room_task_claim("T1", CLAUDE)
    with pytest.raises(ValueError, match="next_checkpoint_seconds"):
        await task_progress.progress(room="main", task_id="T1", agent="claude",
                                    fencing_token=1, result="x",
                                    next_checkpoint_seconds=seconds)


async def test_state_maps_status_and_writes_event(sqlite_db):
    await r.room_task_claim("T1", CLAUDE)
    blocked = await r.room_task_state("T1", 1, CLAUDE, "blocked", "no access")
    assert blocked["task"]["status"] == "blocked"
    unblocked = await r.room_task_state("T1", 1, CLAUDE, "unblocked", "got it")
    assert unblocked["task"]["status"] == "in_progress"
    for state in ("paused", "review"):
        task = (await r.room_task_state("T1", 1, CLAUDE, state, "why"))["task"]
        assert task["status"] == "in_progress"
    assert (await kinds())[-4:] == ["blocked", "unblocked", "paused", "review"]
    assert (await r.room_task_state("T1", 1, CLAUDE, "resumed", "go"))["ok"]


async def test_state_requires_lease(sqlite_db):
    await r.room_task_claim("T1", CLAUDE)
    with pytest.raises(StaleLease):
        await r.room_task_state("T1", 9, CLAUDE, "paused", "x")
    with pytest.raises(StaleLease):
        await r.room_task_state("T1", 1, CODEX, "paused", "x")


async def test_update_metadata_and_list_fields(sqlite_db):
    await r.room_task_claim("T1", CLAUDE)
    refs = [{"kind": "jira", "ref": "SIN-1", "excerpt": "e"}]
    task = (await r.room_task_update("T1", 1, CLAUDE, next_action="run tests", priority=0,
                                     refs=refs))["task"]
    assert (task["next_action"], task["priority"]) == ("run tests", 0)
    assert task["refs"] == refs
    assert task["last_progress_at"] is None
    listed = (await r.room_tasks())["tasks"][0]
    for key in ("priority", "owner", "next_action", "holder_session", "holder_account",
                "last_progress_at", "last_progress_text", "next_checkpoint_at"):
        assert key in listed


@pytest.mark.parametrize("refs", [
    [{"kind": "memory", "ref": "x"}],
    [{"kind": "url", "ref": ""}],
    [{"kind": "url", "ref": "x" * 501}],
    [{"kind": "url", "ref": "x", "excerpt": "e" * 301}],
    [{"kind": "url", "ref": "x", "extra": 1}],
    [{"kind": "url", "ref": "x"}] * 21,
    ["not-an-object"],
])
async def test_refs_validation(sqlite_db, refs):
    await r.room_task_claim("T1", CLAUDE)
    with pytest.raises(ValueError):
        await tasks.update(room="main", task_id="T1", agent="claude", fencing_token=1,
                           refs=refs)


async def test_priority_out_of_range(sqlite_db):
    await r.room_task_claim("T1", CLAUDE)
    with pytest.raises(ValueError, match="priority"):
        await tasks.update(room="main", task_id="T1", agent="claude", fencing_token=1,
                           priority=4)


async def test_history_order_since_and_limit(sqlite_db):
    await r.room_task_claim("T1", CLAUDE)
    for i in range(3):
        await r.room_task_progress("T1", 1, CLAUDE, f"step {i}")
    events = (await r.room_task_history("T1"))["events"]
    ids = [e["id"] for e in events]
    assert ids == sorted(ids) and len(ids) == 5
    after = (await r.room_task_history("T1", since_id=ids[1]))["events"]
    assert [e["id"] for e in after] == ids[2:]
    limited = (await r.room_task_history("T1", limit=2))["events"]
    assert [e["id"] for e in limited] == ids[:2]
    assert (await r.room_task_history("other-task"))["events"] == []
