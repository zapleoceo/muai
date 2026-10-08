"""Комната агентов: сообщения с идемпотентностью и курсором, задачи с арендой и fencing."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from vera_mcp import room_tools as r
from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskRow
from vera_shared.room.tasks import StaleLease, TaskBusy, TaskNotFound

pytestmark = pytest.mark.asyncio


def ctx(client: str):
    return SimpleNamespace(request_context=SimpleNamespace(
        request=SimpleNamespace(scope={"mcp_client": client})))


CLAUDE, CODEX = ctx("claude"), ctx("codex")


async def expire_lease(task_id: str, room: str = "main") -> None:
    async with get_session() as s:
        row = await s.get(RoomTaskRow, (room, task_id))
        row.lease_until = row.lease_until - timedelta(hours=10)


# ─── сообщения ───────────────────────────────────────────────────────────────


async def test_post_author_is_the_token_not_an_argument(sqlite_db):
    res = await r.room_post("hello", CLAUDE, message_id="m1", status="request")
    assert res["deduped"] is False
    assert (res["message"]["from"], res["message"]["status"]) == ("claude", "request")


async def test_same_message_id_is_idempotent(sqlite_db):
    first = await r.room_post("hello", CLAUDE, message_id="m1")
    again = await r.room_post("hello again", CLAUDE, message_id="m1")
    assert again["deduped"] is True
    assert again["message"]["id"] == first["message"]["id"]
    assert len((await r.room_history())["messages"]) == 1


async def test_foreign_message_id_reuse_is_refused(sqlite_db):
    await r.room_post("hello", CLAUDE, message_id="m1")
    with pytest.raises(ValueError, match="already used by claude"):
        await r.room_post("spoof", CODEX, message_id="m1")


async def test_generated_message_id_is_unique(sqlite_db):
    a = await r.room_post("a", CLAUDE)
    b = await r.room_post("b", CLAUDE)
    assert a["message"]["message_id"] != b["message"]["message_id"]


async def test_inbox_shows_others_to_me_or_all_and_ack_moves_cursor(sqlite_db):
    await r.room_post("own", CODEX)
    await r.room_post("broadcast", CLAUDE)
    await r.room_post("direct", CLAUDE, to="codex")
    await r.room_post("not for codex", CLAUDE, to="gemini")
    got = await r.room_inbox(CODEX)
    assert [m["body"] for m in got["messages"]] == ["broadcast", "direct"]
    assert got["cursor"] == got["messages"][-1]["id"]
    assert (await r.room_inbox(CODEX))["messages"] == []


async def test_inbox_without_ack_keeps_cursor(sqlite_db):
    await r.room_post("x", CLAUDE)
    await r.room_inbox(CODEX, ack=False)
    assert [m["body"] for m in (await r.room_inbox(CODEX))["messages"]] == ["x"]


async def test_inbox_since_id_rereads_without_moving_cursor_back(sqlite_db):
    first = (await r.room_post("one", CLAUDE))["message"]["id"]
    await r.room_post("two", CLAUDE)
    await r.room_inbox(CODEX)
    reread = await r.room_inbox(CODEX, since_id=first - 1, ack=True)
    assert [m["body"] for m in reread["messages"]] == ["one", "two"]
    assert (await r.room_inbox(CODEX))["messages"] == []


async def test_rooms_are_separate(sqlite_db):
    await r.room_post("in other", CLAUDE, room="other")
    assert (await r.room_inbox(CODEX))["messages"] == []
    assert len((await r.room_inbox(CODEX, room="other"))["messages"]) == 1


async def test_history_filters_by_task_and_pages_backwards(sqlite_db):
    ids = [(await r.room_post(f"m{i}", CLAUDE, task_id="T" if i % 2 else None))
           ["message"]["id"] for i in range(4)]
    assert [m["body"] for m in (await r.room_history(task_id="T"))["messages"]] == [
        "m1", "m3"]
    assert [m["body"] for m in (await r.room_history(before_id=ids[2]))["messages"]] == [
        "m0", "m1"]


# ─── задачи ──────────────────────────────────────────────────────────────────


async def test_claim_gives_fencing_and_blocks_others(sqlite_db):
    task = (await r.room_task_claim("T1", CLAUDE, title="room", paths=["a.py"]))["task"]
    assert (task["lease_holder"], task["fencing_token"], task["status"]) == (
        "claude", 1, "in_progress")
    with pytest.raises(TaskBusy, match="held by claude"):
        await r.room_task_claim("T1", CODEX)


async def test_reclaim_by_holder_renews_without_new_token(sqlite_db):
    await r.room_task_claim("T1", CLAUDE)
    again = (await r.room_task_claim("T1", CLAUDE, lease_seconds=3600))["task"]
    assert again["fencing_token"] == 1


async def test_expired_lease_is_taken_over_and_old_holder_is_fenced(sqlite_db):
    await r.room_task_claim("T1", CLAUDE)
    await expire_lease("T1")
    taken = (await r.room_task_claim("T1", CODEX))["task"]
    assert (taken["lease_holder"], taken["fencing_token"]) == ("codex", 2)
    with pytest.raises(StaleLease):
        await r.room_task_update("T1", 1, CLAUDE, note="woke up late")
    with pytest.raises(StaleLease):
        await r.room_task_release("T1", 1, CLAUDE)


async def test_expired_lease_rejects_even_the_same_token(sqlite_db):
    await r.room_task_claim("T1", CLAUDE)
    await expire_lease("T1")
    with pytest.raises(StaleLease):
        await r.room_task_update("T1", 1, CLAUDE, note="too late")


async def test_update_and_release_with_current_token(sqlite_db):
    await r.room_task_claim("T1", CLAUDE)
    upd = (await r.room_task_update("T1", 1, CLAUDE, status="blocked", note="waiting",
                                    extend_seconds=600))["task"]
    assert (upd["status"], upd["note"]) == ("blocked", "waiting")
    done = (await r.room_task_release("T1", 1, CLAUDE, note="merged"))["task"]
    assert (done["status"], done["lease_holder"]) == ("done", None)
    with pytest.raises(TaskBusy, match="is done"):
        await r.room_task_claim("T1", CODEX)


async def test_released_to_open_can_be_claimed_by_another(sqlite_db):
    await r.room_task_claim("T1", CLAUDE)
    await r.room_task_release("T1", 1, CLAUDE, status="open")
    assert (await r.room_task_claim("T1", CODEX))["task"]["fencing_token"] == 2


async def test_update_unknown_task(sqlite_db):
    with pytest.raises(TaskNotFound):
        await r.room_task_update("nope", 1, CLAUDE)


async def test_open_task_is_unclaimed_and_listed(sqlite_db):
    opened = await r.room_task_open("T2", CODEX, title="review", paths=["x"])
    assert opened["created"] is True
    assert (await r.room_task_open("T2", CLAUDE))["created"] is False
    listed = (await r.room_tasks(status="open"))["tasks"]
    assert [(t["task_id"], t["created_by"], t["lease_holder"]) for t in listed] == [
        ("T2", "codex", None)]
    await r.room_task_claim("T2", CLAUDE)
    assert (await r.room_tasks(status="open"))["tasks"] == []
