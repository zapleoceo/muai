"""Правило состояния S1-S7 (дизайн room-stage1b v10, §2.7): ST1-ST4, ST8, ST11, ST12.

Продление аренды меняет только аренду; содержательные операции держателя не снимают
ожидание владельца; при блоке владельца (OB) исполнитель сам блок не снимает.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskQuestionRow, RoomTaskRow
from vera_shared.room import handoff, questions, task_progress, task_wait, tasks
from vera_shared.room.owner_block import (
    BY_HOLDER,
    BY_OWNER,
    OwnerBlock,
    fold_pauses,
    owner_block,
    pause_actor,
    pause_state,
)
from vera_shared.room.task_events import record_event
from vera_shared.timeutil import utc_naive_now

pytestmark = pytest.mark.asyncio
R, T = "main", "S1"


async def claim(agent: str = "claude") -> dict:
    return await tasks.claim(room=R, task_id=T, agent=agent, lease_seconds=600)


async def row() -> RoomTaskRow:
    async with get_session() as s:
        r = await s.get(RoomTaskRow, (R, T))
        s.expunge(r)
        return r


async def expire() -> None:
    async with get_session() as s:
        (await s.get(RoomTaskRow, (R, T))).lease_until = utc_naive_now() - timedelta(seconds=5)


async def kinds() -> list[str]:
    events = await task_progress.history(room=R, task_id=T, since_id=None, limit=200)
    return [e["kind"] for e in events]


async def events_of(kind: str) -> list[dict]:
    events = await task_progress.history(room=R, task_id=T, since_id=None, limit=200)
    return [e for e in events if e["kind"] == kind]


async def ask(tok: int) -> int:
    return (await questions.ask(room=R, task_id=T, agent="claude", fencing_token=tok,
                                question="?"))["qid"]


async def wait_owner(tok: int, label: str | None = "owner") -> None:
    await task_wait.wait(room=R, task_id=T, agent="claude", fencing_token=tok,
                         until_seconds=3600, reason="ждём владельца", decision_owner=label)


async def owner_pause_event(kind: str = "paused") -> None:
    async with get_session() as s:
        await record_event(s, room=R, task_id=T, kind=kind, agent="owner",
                           data={"by": BY_OWNER})


async def ob() -> list[str]:
    async with get_session() as s:
        return (await owner_block(s, await s.get(RoomTaskRow, (R, T)))).reasons()


async def set_state(tok: int, state: str) -> dict:
    return await task_progress.set_state(room=R, task_id=T, agent="claude",
                                         fencing_token=tok, state=state, reason="x")


async def release(tok: int, status: str) -> dict:
    return await tasks.release(room=R, task_id=T, agent="claude", fencing_token=tok,
                               status=status)


async def update(tok: int, **kw) -> dict:
    return await tasks.update(room=R, task_id=T, agent="claude", fencing_token=tok, **kw)


# ---- S1 / ST1 -------------------------------------------------------------------------

async def test_st1_extension_keeps_blocked_owner_and_open_question(sqlite_db):
    tok = (await claim())["fencing_token"]
    await ask(tok)
    for extend in (claim, None):
        if extend:
            await extend()
        else:
            await update(tok, extend_seconds=900)
        after = await row()
        assert (after.status, after.owner, after.fencing_token) == ("blocked", "owner", tok)
    assert await ob() == ["owner_question"]
    assert (await kinds()).count("heartbeat") == 2


async def test_st1_new_lease_after_expiry_keeps_blocked_and_question(sqlite_db):
    tok = (await claim())["fencing_token"]
    qid = await ask(tok)
    await questions.answer(room=R, task_id=T, qid=qid, text="да")  # open claim — owner_question (B1)
    await expire()
    got = await claim()
    assert got["fencing_token"] == tok + 1  # истёкшая аренда — новая (ключ K вне B0a)
    after = await row()
    assert (after.status, after.owner) == ("blocked", "owner")
    assert await ob() == ["owner_question"]  # answered без ack


async def test_s2_new_lease_moves_open_to_in_progress_only(sqlite_db):
    assert (await claim())["status"] == "in_progress"
    await release(1, "open")
    assert (await claim("codex"))["status"] == "in_progress"
    await tasks.release(room=R, task_id=T, agent="codex", fencing_token=2, status="blocked")
    assert (await claim("claude"))["status"] == "blocked"  # blocked — до явного события (S7)


# ---- S3 / ST2 / ST11 ------------------------------------------------------------------

async def test_st2_owner_wait_survives_extension_and_progress(sqlite_db):
    tok = (await claim())["fencing_token"]
    await wait_owner(tok)
    await claim()
    await update(tok, extend_seconds=900)
    got = await task_progress.progress(room=R, task_id=T, agent="claude",
                                       fencing_token=tok, result="сделал часть")
    assert got["waiting_decision_owner"] == "owner" and got["waiting_until"]
    assert got["last_progress_text"] == "сделал часть" and got["last_progress_at"]
    upd = await update(tok, note="заметка")
    assert upd["waiting_decision_owner"] == "owner"
    assert upd["last_progress_text"] == "заметка"
    assert await ob() == ["owner_wait"]


async def test_st2_owner_wait_blocks_new_lease_and_stays(sqlite_db):
    tok = (await claim())["fencing_token"]
    await wait_owner(tok)
    await expire()
    with pytest.raises(tasks.TaskBusy, match="owner_wait"):  # ворота B1
        await claim("codex")
    after = await row()
    assert after.waiting_decision_owner == "owner" and after.waiting_until
    assert after.fencing_token == tok


async def test_st11_holder_wait_is_cleared_by_progress(sqlite_db):
    tok = (await claim())["fencing_token"]
    await wait_owner(tok, None)  # диспетчер: ожидание без decision_owner
    assert await ob() == []
    got = await task_progress.progress(room=R, task_id=T, agent="claude",
                                       fencing_token=tok, result="CI зелёный")
    assert (got["waiting_until"], got["waiting_reason"],
            got["waiting_decision_owner"]) == (None, None, None)


async def test_holder_wait_cannot_replace_owner_wait(sqlite_db):
    tok = (await claim())["fencing_token"]
    await wait_owner(tok)
    with pytest.raises(OwnerBlock):
        await wait_owner(tok, None)
    with pytest.raises(OwnerBlock):
        await wait_owner(tok, "dot")
    await wait_owner(tok)  # продлить ожидание владельца можно
    assert await ob() == ["owner_wait"]


# ---- S4 / ST3 / ST4 ------------------------------------------------------------------

async def test_st3_release_with_owner_wait(sqlite_db):
    tok = (await claim())["fencing_token"]
    await wait_owner(tok)
    for status in ("open", "done"):
        with pytest.raises(OwnerBlock):
            await release(tok, status)
    got = await release(tok, "blocked")
    assert got["status"] == "blocked" and got["lease_holder"] is None
    assert got["waiting_decision_owner"] == "owner" and got["waiting_until"]


async def test_release_with_question_keeps_it_and_without_ob_is_unchanged(sqlite_db):
    tok = (await claim())["fencing_token"]
    qid = await ask(tok)
    with pytest.raises(OwnerBlock):
        await release(tok, "open")
    await release(tok, "blocked")
    assert [q["qid"] for q in await questions.questions_of(R, T)] == [qid]
    # без OB: ожидание держателя по-прежнему чистится, done проходит
    tok2 = (await tasks.claim(room=R, task_id="S2", agent="claude",
                              lease_seconds=600))["fencing_token"]
    await task_wait.wait(room=R, task_id="S2", agent="claude", fencing_token=tok2,
                         until_seconds=3600, reason="CI")
    done = await tasks.release(room=R, task_id="S2", agent="claude", fencing_token=tok2,
                               status="done")
    assert done["status"] == "done" and done["waiting_until"] is None


async def test_st4_holder_cannot_lift_question_block(sqlite_db):
    tok = (await claim())["fencing_token"]
    await ask(tok)
    with pytest.raises(OwnerBlock):
        await update(tok, status="in_progress")
    for state in ("unblocked", "resumed"):
        with pytest.raises(OwnerBlock):
            await set_state(tok, state)
    assert (await row()).status == "blocked"
    # разрешено: пауза держателя, передача, blocked, заметка
    await set_state(tok, "paused")
    await handoff.offer(room=R, task_id=T, agent="claude", fencing_token=tok,
                        to_agent="codex")
    await update(tok, status="blocked", note="всё ещё жду")


async def test_st4_owner_wait_and_owner_pause_also_block(sqlite_db):
    tok = (await claim())["fencing_token"]
    await wait_owner(tok)
    with pytest.raises(OwnerBlock):
        await update(tok, status="in_progress")
    await owner_pause_event()
    assert await ob() == ["owner_wait", "owner_pause"]
    with pytest.raises(OwnerBlock):
        await set_state(tok, "resumed")
    with pytest.raises(OwnerBlock):
        await release(tok, "done")
    await release(tok, "blocked")
    assert await ob() == ["owner_wait", "owner_pause"]  # OB пережил release(blocked)


async def test_no_ob_holder_flow_is_unchanged(sqlite_db):
    tok = (await claim())["fencing_token"]
    await set_state(tok, "blocked")
    got = await update(tok, status="in_progress")
    assert got["status"] == "in_progress"
    assert "unblocked" in await kinds()


# ---- ST8 ----------------------------------------------------------------------------

async def test_st8_answer_then_ack_lifts_question_block(sqlite_db):
    tok = (await claim())["fencing_token"]
    qid = await ask(tok)
    await questions.answer(room=R, task_id=T, qid=qid, text="да")
    assert await ob() == ["owner_question"]  # answered без ack — тоже OB
    await questions.ack(room=R, task_id=T, qid=qid, agent="claude", fencing_token=tok)
    after = await row()
    assert (after.status, after.owner) == ("in_progress", None)
    assert await ob() == []
    assert "unblocked" in await kinds()


# ---- S6: accept ---------------------------------------------------------------------

async def test_accept_refuses_owner_wait_and_leaves_state_untouched(sqlite_db):
    tok = (await claim())["fencing_token"]
    await wait_owner(tok)
    await set_state(tok, "blocked")
    await handoff.offer(room=R, task_id=T, agent="claude", fencing_token=tok,
                        to_agent="codex")
    with pytest.raises(handoff.HandoffError, match="owner_wait"):  # ворота B1
        await handoff.accept(room=R, task_id=T, agent="codex")
    after = await row()
    assert after.status == "blocked" and after.lease_holder == "claude"
    assert after.waiting_decision_owner == "owner" and after.pending_handoff_to == "codex"


async def test_accept_without_ob_clears_holder_wait(sqlite_db):
    tok = (await claim())["fencing_token"]
    await task_wait.wait(room=R, task_id=T, agent="claude", fencing_token=tok,
                         until_seconds=3600, reason="CI")
    await handoff.offer(room=R, task_id=T, agent="claude", fencing_token=tok,
                        to_agent="codex")
    got = await handoff.accept(room=R, task_id=T, agent="codex")
    assert got["status"] == "in_progress" and got["waiting_until"] is None


# ---- ST12 и предикат OB -------------------------------------------------------------

async def test_st12_legacy_pause_without_by_is_holder_pause(sqlite_db):
    tok = (await claim())["fencing_token"]
    async with get_session() as s:
        await record_event(s, room=R, task_id=T, kind="paused", agent="claude")  # легаси
    async with get_session() as s:
        assert await pause_state(s, R, T) == {BY_HOLDER: True, BY_OWNER: False}
    assert await ob() == []
    await set_state(tok, "resumed")  # пауза держателя снимается его же resumed
    async with get_session() as s:
        assert await pause_state(s, R, T) == {BY_HOLDER: False, BY_OWNER: False}
    assert (await events_of("resumed"))[0]["data"] == {"by": "holder"}


async def test_set_state_paused_carries_by_holder_and_blocks_accept(sqlite_db):
    tok = (await claim())["fencing_token"]
    await set_state(tok, "paused")
    assert (await events_of("paused"))[0]["data"] == {"by": "holder"}
    await handoff.offer(room=R, task_id=T, agent="claude", fencing_token=tok,
                        to_agent="codex")
    with pytest.raises(handoff.HandoffError):
        await handoff.accept(room=R, task_id=T, agent="codex")


async def test_owner_pause_predicate_with_fixture_event(sqlite_db):
    tok = (await claim())["fencing_token"]
    await owner_pause_event("paused")
    assert await ob() == ["owner_pause"]
    with pytest.raises(OwnerBlock):  # resumed держателя паузу владельца не снимает
        await set_state(tok, "resumed")
    async with get_session() as s:
        assert await pause_state(s, R, T) == {BY_HOLDER: False, BY_OWNER: True}
    await owner_pause_event("resumed")
    assert await ob() == []


async def test_question_predicate_counts_open_and_answered_not_acked(sqlite_db):
    tok = (await claim())["fencing_token"]
    qid = await ask(tok)
    for status, expected in (("open", ["owner_question"]), ("answered", ["owner_question"]),
                             ("acked", []), ("withdrawn", [])):
        async with get_session() as s:
            (await s.get(RoomTaskQuestionRow, qid)).status = status
        assert await ob() == expected


def test_fold_pauses_pure():
    h, o = BY_HOLDER, BY_OWNER
    assert fold_pauses([]) == {h: False, o: False}
    assert fold_pauses([("paused", None)]) == {h: True, o: False}
    assert fold_pauses([("paused", {"by": o}), ("resumed", {"by": h})]) == {h: False, o: True}
    assert fold_pauses([("paused", {"by": o}), ("resumed", {"by": o}),
                        ("paused", {"by": h})]) == {h: True, o: False}
    assert fold_pauses([("progress", {"by": o})]) == {h: False, o: False}
    assert pause_actor({"by": "owner"}) == o
    assert pause_actor({"by": "garbage"}) == h and pause_actor({}) == h


# ---- S5/S7: ack закрывает вопрос, но не чужие блоки владельца --------------------------

async def answered_question(tok: int) -> int:
    qid = await ask(tok)
    await questions.answer(room=R, task_id=T, qid=qid, text="да")
    return qid


async def ack(tok: int, qid: int) -> None:
    await questions.ack(room=R, task_id=T, qid=qid, agent="claude", fencing_token=tok)


async def test_ack_with_owner_wait_keeps_blocked_and_writes_no_unblocked(sqlite_db):
    tok = (await claim())["fencing_token"]
    await wait_owner(tok)
    qid = await answered_question(tok)
    await ack(tok, qid)
    after = await row()
    assert after.status == "blocked" and after.waiting_decision_owner == "owner"
    assert await ob() == ["owner_wait"]
    assert "unblocked" not in await kinds()


async def test_ack_with_owner_pause_keeps_blocked_and_writes_no_unblocked(sqlite_db):
    tok = (await claim())["fencing_token"]
    qid = await answered_question(tok)
    await owner_pause_event()
    await ack(tok, qid)
    assert (await row()).status == "blocked"
    assert await ob() == ["owner_pause"]
    assert "unblocked" not in await kinds()


async def test_withdraw_with_owner_wait_keeps_blocked(sqlite_db):
    tok = (await claim())["fencing_token"]
    await wait_owner(tok)
    qid = await ask(tok)
    await questions.withdraw(room=R, task_id=T, qid=qid, agent="claude", fencing_token=tok)
    assert (await row()).status == "blocked"
    assert "unblocked" not in await kinds()


async def test_ack_unblocks_when_the_question_was_the_only_block(sqlite_db):
    tok = (await claim())["fencing_token"]
    qid = await answered_question(tok)
    await ack(tok, qid)
    assert (await row()).status == "in_progress"
    assert (await kinds()).count("unblocked") == 1


async def test_ack_unblocks_after_owner_pause_is_resumed(sqlite_db):
    tok = (await claim())["fencing_token"]
    qid = await answered_question(tok)
    await owner_pause_event()
    await owner_pause_event("resumed")
    await ack(tok, qid)
    assert (await row()).status == "in_progress"
