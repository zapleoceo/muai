"""Трекер задач, шаг 1: события пишутся в той же транзакции, heartbeat не двигает прогресс."""
from __future__ import annotations

import pytest
from vera_shared.db.models_room import EVENT_KINDS, RoomTaskRow
from vera_shared.room import tasks
from vera_shared.room.task_events import list_events, record_event

pytestmark = pytest.mark.asyncio


async def kinds(get_session, task_id: str = "T") -> list[str]:
    async with get_session() as s:
        return [e["kind"] for e in await list_events(s, room="main", task_id=task_id)]


async def row(get_session, task_id: str = "T") -> RoomTaskRow:
    async with get_session() as s:
        return await s.get(RoomTaskRow, ("main", task_id))


async def test_open_and_claim_write_created_then_claimed(sqlite_db):
    await tasks.open_task(room="main", task_id="T", agent="claude", title="t", paths=None)
    await tasks.claim(room="main", task_id="T", agent="claude", lease_seconds=60)
    assert await kinds(sqlite_db) == ["created", "claimed"]


async def test_claim_of_new_task_writes_created_and_claimed(sqlite_db):
    await tasks.claim(room="main", task_id="T", agent="claude", lease_seconds=60)
    assert await kinds(sqlite_db) == ["created", "claimed"]


async def test_reclaim_by_holder_is_heartbeat(sqlite_db):
    await tasks.claim(room="main", task_id="T", agent="claude", lease_seconds=60)
    await tasks.claim(room="main", task_id="T", agent="claude", lease_seconds=120)
    assert await kinds(sqlite_db) == ["created", "claimed", "heartbeat"]


async def test_extend_only_is_heartbeat_and_leaves_progress_untouched(sqlite_db):
    t = await tasks.claim(room="main", task_id="T", agent="claude", lease_seconds=60)
    await tasks.update(room="main", task_id="T", agent="claude",
                       fencing_token=t["fencing_token"], extend_seconds=300)
    assert (await kinds(sqlite_db))[-1] == "heartbeat"
    r = await row(sqlite_db)
    assert r.last_progress_at is None and r.last_progress_text is None


async def test_note_or_status_is_progress_and_sets_last_progress(sqlite_db):
    t = await tasks.claim(room="main", task_id="T", agent="claude", lease_seconds=60)
    await tasks.update(room="main", task_id="T", agent="claude",
                       fencing_token=t["fencing_token"], note="half done")
    r = await row(sqlite_db)
    assert (await kinds(sqlite_db))[-1] == "progress"
    assert r.last_progress_text == "half done" and r.last_progress_at is not None
    stamp = r.last_progress_at
    await tasks.update(room="main", task_id="T", agent="claude",
                       fencing_token=t["fencing_token"], extend_seconds=300)
    assert (await row(sqlite_db)).last_progress_at == stamp


async def test_status_without_note_is_progress(sqlite_db):
    t = await tasks.claim(room="main", task_id="T", agent="claude", lease_seconds=60)
    await tasks.update(room="main", task_id="T", agent="claude",
                       fencing_token=t["fencing_token"], status="blocked")
    assert (await kinds(sqlite_db))[-1] == "progress"
    assert (await row(sqlite_db)).last_progress_text == "status: blocked"


@pytest.mark.parametrize(("status", "kind"), [("done", "done"), ("open", "released"),
                                              ("blocked", "released")])
async def test_release_kind_follows_status(sqlite_db, status, kind):
    t = await tasks.claim(room="main", task_id="T", agent="claude", lease_seconds=60)
    await tasks.release(room="main", task_id="T", agent="claude",
                        fencing_token=t["fencing_token"], status=status)
    assert (await kinds(sqlite_db))[-1] == kind


async def test_claim_stores_session_and_account_and_legacy_call_still_works(sqlite_db):
    await tasks.claim(room="main", task_id="A", agent="claude", lease_seconds=60,
                      session="laptop", account="acc1")
    r = await row(sqlite_db, "A")
    assert (r.holder_session, r.holder_account) == ("laptop", "acc1")
    await tasks.claim(room="main", task_id="B", agent="claude", lease_seconds=60)
    r = await row(sqlite_db, "B")
    assert (r.holder_session, r.holder_account) == (None, None)


async def test_failed_update_leaves_no_event(sqlite_db):
    await tasks.claim(room="main", task_id="T", agent="claude", lease_seconds=60)
    with pytest.raises(tasks.StaleLease):
        await tasks.update(room="main", task_id="T", agent="claude", fencing_token=99,
                           note="x")
    assert await kinds(sqlite_db) == ["created", "claimed"]


async def test_unknown_kind_rejected_and_all_kinds_accepted(sqlite_db):
    async with sqlite_db() as s:
        with pytest.raises(ValueError, match="unknown"):
            await record_event(s, room="main", task_id="T", kind="bogus")
        for k in EVENT_KINDS:
            await record_event(s, room="main", task_id="T", kind=k)
    assert len(EVENT_KINDS) == 18
