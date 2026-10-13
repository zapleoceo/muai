"""room_task_cancel: держатель или создатель свободной задачи, без захвата и без in_progress."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from vera_mcp import room_tools as r
from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskRow
from vera_shared.room.tasks import StaleLease, TaskBusy

pytestmark = pytest.mark.asyncio

SECRET = "token: ghp_abcdefghijklmnopqrstuvwxyz0123456789"
LEAK = "ghp_abcdefghijklmnopqrstuvwxyz"


def ctx(client: str):
    return SimpleNamespace(request_context=SimpleNamespace(
        request=SimpleNamespace(scope={"mcp_client": client})))


CLAUDE, CODEX = ctx("claude"), ctx("codex")


async def kinds(task_id: str = "T1") -> list[str]:
    return [e["kind"] for e in (await r.room_task_history(task_id))["events"]]


async def blocked_unclaimed(task_id: str = "T1") -> int:
    """Создана claude, прошла захват и blocked-релиз: статус blocked, аренды нет."""
    await r.room_task_open(task_id, CLAUDE, title="Т", project="Vera")
    token = (await r.room_task_claim(task_id, CLAUDE))["task"]["fencing_token"]
    await r.room_task_progress(task_id, token, CLAUDE, result="сделал шаг")
    await r.room_task_release(task_id, token, CLAUDE, status="blocked", note="жду")
    return token


async def test_creator_cancels_blocked_unclaimed_task_without_claim(sqlite_db):
    token = await blocked_unclaimed()
    before = await kinds()
    res = await r.room_task_cancel("T1", token, CLAUDE, reason="не нужно")
    task = res["task"]
    assert (task["status"], task["note"]) == ("cancelled", "не нужно")
    assert task["lease_holder"] is None and task["fencing_token"] == token
    after = (await kinds())[len(before):]
    assert after == ["cancelled"]
    assert "claimed" not in after


async def test_creator_cancels_never_claimed_open_task_with_token_zero(sqlite_db):
    await r.room_task_open("T1", CLAUDE)
    res = await r.room_task_cancel("T1", 0, CLAUDE, reason="дубль")
    assert res["task"]["status"] == "cancelled"
    assert await kinds() == ["created", "cancelled"]


async def test_holder_cancels_in_progress_and_prior_status_logged(sqlite_db):
    token = (await r.room_task_claim("T1", CODEX))["task"]["fencing_token"]
    await r.room_task_cancel("T1", token, CODEX, reason="отмена")
    event = [e for e in (await r.room_task_history("T1"))["events"]
             if e["kind"] == "cancelled"][0]
    assert event["data"]["prior_status"] == "in_progress"
    assert event["agent"] == "codex" and event["fencing_token"] == token


async def test_non_creator_non_holder_is_rejected(sqlite_db):
    token = await blocked_unclaimed()
    with pytest.raises(StaleLease):
        await r.room_task_cancel("T1", token, CODEX, reason="чужая")
    assert (await r.room_tasks())["tasks"][0]["status"] == "blocked"


async def test_creator_rejected_while_someone_else_holds_live_lease(sqlite_db):
    await r.room_task_open("T1", CLAUDE)
    token = (await r.room_task_claim("T1", CODEX))["task"]["fencing_token"]
    with pytest.raises(StaleLease):
        await r.room_task_cancel("T1", token, CLAUDE, reason="перехват")
    assert (await r.room_tasks())["tasks"][0]["status"] == "in_progress"


async def test_stale_fencing_is_rejected(sqlite_db):
    token = await blocked_unclaimed()
    with pytest.raises(StaleLease):
        await r.room_task_cancel("T1", token + 1, CLAUDE, reason="устарел")
    with pytest.raises(StaleLease):
        await r.room_task_cancel("T1", 0, CLAUDE, reason="устарел")


async def test_done_and_cancelled_are_rejected(sqlite_db):
    await r.room_task_open("T1", CLAUDE)
    token = (await r.room_task_claim("T1", CLAUDE))["task"]["fencing_token"]
    await r.room_task_release("T1", token, CLAUDE, status="done")
    with pytest.raises(TaskBusy, match="already done"):
        await r.room_task_cancel("T1", token, CLAUDE, reason="поздно")
    token = await blocked_unclaimed("T2")
    await r.room_task_cancel("T2", token, CLAUDE, reason="раз")
    with pytest.raises(TaskBusy, match="already cancelled"):
        await r.room_task_cancel("T2", token, CLAUDE, reason="два")


async def test_no_in_progress_transition_and_execution_fields_preserved(sqlite_db):
    token = await blocked_unclaimed()
    async with get_session() as s:
        row = await s.get(RoomTaskRow, ("main", "T1"))
        before = (row.last_progress_at, row.last_progress_text, row.refs, row.project,
                  row.next_action, row.fencing_token, row.title)
    events_before = await r.room_task_history("T1")
    await r.room_task_cancel("T1", token, CLAUDE, reason="нет")
    async with get_session() as s:
        row = await s.get(RoomTaskRow, ("main", "T1"))
        after = (row.last_progress_at, row.last_progress_text, row.refs, row.project,
                 row.next_action, row.fencing_token, row.title)
    assert after == before and before[1] == "сделал шаг"
    new = (await r.room_task_history("T1"))["events"][len(events_before["events"]):]
    assert [e["kind"] for e in new] == ["cancelled"]
    assert all((e["data"] or {}).get("status") != "in_progress" for e in new)


async def test_cancelled_task_cannot_be_reclaimed(sqlite_db):
    token = await blocked_unclaimed()
    await r.room_task_cancel("T1", token, CLAUDE, reason="нет")
    with pytest.raises(TaskBusy, match="cancelled"):
        await r.room_task_claim("T1", CODEX)


async def test_reason_redacted_in_view_and_history(sqlite_db):
    token = await blocked_unclaimed()
    res = await r.room_task_cancel("T1", token, CLAUDE, reason=f"дубль {SECRET}")
    assert LEAK not in res["task"]["note"]
    event = [e for e in (await r.room_task_history("T1"))["events"]
             if e["kind"] == "cancelled"][0]
    assert LEAK not in event["text"] and LEAK not in str(event["data"])


async def test_tool_is_registered_last_without_reordering_existing_ones():
    names = [t.__name__ for t in r.ROOM_TOOLS]
    assert names[-1] == "room_task_cancel"
    assert names[:14] == ["room_post", "room_inbox", "room_ack", "room_history",
                          "room_task_open", "room_task_claim", "room_task_update",
                          "room_task_progress", "room_task_state", "room_task_history",
                          "room_task_wait", "room_task_next", "room_task_release",
                          "room_tasks"]
