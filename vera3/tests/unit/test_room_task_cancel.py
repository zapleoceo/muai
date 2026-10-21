"""room_task_cancel: держатель или создатель свободной задачи, без захвата и без in_progress."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from vera_mcp import room_question_tools as qt
from vera_mcp import room_tools as r
from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskRow
from vera_shared.room import handoff, questions
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
    assert names[-2:] == ["room_message_get", "room_task_cancel"]
    assert names[:14] == ["room_post", "room_inbox", "room_ack", "room_history",
                          "room_task_open", "room_task_claim", "room_task_update",
                          "room_task_progress", "room_task_state", "room_task_history",
                          "room_task_wait", "room_task_next", "room_task_release",
                          "room_tasks"]


async def expire_lease(task_id: str) -> None:
    async with get_session() as s:
        row = await s.get(RoomTaskRow, ("main", task_id))
        row.lease_until = row.lease_until - timedelta(hours=10)


async def test_creator_cancels_after_holder_lease_expired(sqlite_db):
    await r.room_task_open("T1", CLAUDE)
    token = (await r.room_task_claim("T1", CODEX))["task"]["fencing_token"]
    await expire_lease("T1")
    res = await r.room_task_cancel("T1", token, CLAUDE, reason="истекла")
    assert res["task"]["status"] == "cancelled"
    event = [e for e in (await r.room_task_history("T1"))["events"]
             if e["kind"] == "cancelled"][0]
    assert event["agent"] == "claude" and event["session"] is None
    assert event["data"]["prior_status"] == "in_progress"


async def test_non_creator_ex_holder_refused_after_expiry(sqlite_db):
    await r.room_task_open("T1", CLAUDE)
    token = (await r.room_task_claim("T1", CODEX))["task"]["fencing_token"]
    await expire_lease("T1")
    with pytest.raises(StaleLease):
        await r.room_task_cancel("T1", token, CODEX, reason="поздно")


async def test_holder_event_carries_callers_session(sqlite_db):
    token = (await r.room_task_claim("T1", CODEX, session="laptop"))["task"]["fencing_token"]
    await r.room_task_cancel("T1", token, CODEX, reason="отмена")
    event = [e for e in (await r.room_task_history("T1"))["events"]
             if e["kind"] == "cancelled"][0]
    assert event["session"] == "laptop"


async def test_pending_handoff_is_cleared(sqlite_db):
    token = (await r.room_task_claim("T1", CLAUDE))["task"]["fencing_token"]
    await handoff.offer(room="main", task_id="T1", agent="claude", fencing_token=token,
                        to_agent="codex", note=None)
    async with get_session() as s:
        assert (await s.get(RoomTaskRow, ("main", "T1"))).pending_handoff_to == "codex"
    await r.room_task_cancel("T1", token, CLAUDE, reason="не нужно")
    async with get_session() as s:
        assert (await s.get(RoomTaskRow, ("main", "T1"))).pending_handoff_to is None


async def test_cancel_from_waiting_and_paused(sqlite_db):
    token = (await r.room_task_claim("W", CLAUDE))["task"]["fencing_token"]
    await r.room_task_wait("W", token, CLAUDE, until_seconds=600, reason="жду")
    assert (await r.room_task_cancel("W", token, CLAUDE, reason="x"))["task"][
        "status"] == "cancelled"
    token = (await r.room_task_claim("P", CLAUDE))["task"]["fencing_token"]
    await r.room_task_state("P", token, CLAUDE, state="paused", reason="пауза")
    assert (await r.room_task_cancel("P", token, CLAUDE, reason="x"))["task"][
        "status"] == "cancelled"


async def test_attested_task_is_refused(sqlite_db, monkeypatch):
    from vera_shared.room import task_cancel

    async def attested(*_a):
        return object()

    await r.room_task_open("T1", CLAUDE)
    monkeypatch.setattr(task_cancel, "attestation_for", attested)
    with pytest.raises(TaskBusy, match="attested"):
        await r.room_task_cancel("T1", 0, CLAUDE, reason="x")
    assert (await r.room_tasks())["tasks"][0]["status"] == "open"


async def test_cancelled_hidden_from_open_queues_but_listable_by_status(sqlite_db):
    await r.room_task_open("keep", CLAUDE)
    await r.room_task_open("gone", CLAUDE)
    await r.room_task_cancel("gone", 0, CLAUDE, reason="x")
    for queue in ("open", "unclaimed"):
        ids = [t["task_id"] for t in (await r.room_tasks(queue=queue))["tasks"]]
        assert ids == ["keep"]
    listed = (await r.room_tasks(status="cancelled"))["tasks"]
    assert [t["task_id"] for t in listed] == ["gone"]


async def test_dependent_of_cancelled_task_stays_ineligible(sqlite_db):
    await r.room_task_open("dep", CLAUDE)
    await r.room_task_open("child", CLAUDE, depends_on=["dep"], auto_pickup=True)
    await r.room_task_cancel("dep", 0, CLAUDE, reason="x")
    assert (await r.room_task_next(CODEX))["task"] is None


async def test_cancel_withdraws_unresolved_questions_and_keeps_answers(sqlite_db):
    token = (await r.room_task_claim("T1", CLAUDE))["task"]["fencing_token"]
    qids = [(await qt.room_task_ask("T1", token, CLAUDE, f"question {i}"))[
        "question"]["qid"] for i in range(3)]
    await questions.answer(room="main", task_id="T1", qid=qids[1], text="owner answer")
    await questions.answer(room="main", task_id="T1", qid=qids[2], text="kept answer")
    await qt.room_task_answer_ack("T1", token, qids[2], CLAUDE)
    await r.room_task_release("T1", token, CLAUDE, status="blocked", note="waiting")

    await r.room_task_cancel("T1", token, CLAUDE, reason="obsolete by owner decision")

    got = (await qt.room_task_questions("T1"))["questions"]
    assert [q["status"] for q in got] == ["withdrawn", "withdrawn", "acked"]
    assert got[1]["answers"][0]["text"] == "owner answer"
    assert got[2]["answers"][0]["text"] == "kept answer"
    event = (await r.room_task_history("T1"))["events"][-1]
    assert event["kind"] == "cancelled"
    assert event["data"]["withdrawn_qids"] == qids[:2]
    assert event["data"]["reason"] == "obsolete by owner decision"


async def test_rejected_cancel_does_not_withdraw_question(sqlite_db):
    token = (await r.room_task_claim("T1", CLAUDE))["task"]["fencing_token"]
    qid = (await qt.room_task_ask("T1", token, CLAUDE, "question"))["question"]["qid"]
    await r.room_task_release("T1", token, CLAUDE, status="blocked", note="waiting")

    with pytest.raises(StaleLease):
        await r.room_task_cancel("T1", token, CODEX, reason="unauthorized")

    assert (await qt.room_task_questions("T1"))["questions"][0]["status"] == "open"
    assert all(qid not in (e["data"] or {}).get("withdrawn_qids", [])
               for e in (await r.room_task_history("T1"))["events"])
