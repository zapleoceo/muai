"""Действия владельца B1: recover/start, закрытие вопроса, wait/resolve (G3-G5, ST9, ST10, ST13-ST15, ST20, ST22)."""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from vera_shared.db.engine import get_session
from vera_shared.db.models_room import (
    RoomTaskAnswerRow,
    RoomTaskQuestionRow,
    RoomTaskRow,
)
from vera_shared.room import owner_actions, questions, recovery, task_progress, tasks
from vera_shared.room.owner_block import OwnerBlock
from vera_shared.room.task_wait import wait
from vera_shared.timeutil import utc_naive_now

pytestmark = pytest.mark.asyncio
R, T = "main", "RC1"
PROOF = {"stop_source": "process_absent_observed", "effects": "none_known",
         "residual_risk_ack": True}


async def claim(agent: str = "claude", session: str | None = "sess-1") -> dict:
    return await tasks.claim(room=R, task_id=T, agent=agent, lease_seconds=600, session=session)


async def row() -> RoomTaskRow:
    async with get_session() as s:
        r = await s.get(RoomTaskRow, (R, T))
        s.expunge(r)
        return r


async def expire() -> None:
    async with get_session() as s:
        (await s.get(RoomTaskRow, (R, T))).lease_until = utc_naive_now() - timedelta(seconds=5)


async def events(kind: str) -> list[dict]:
    got = await task_progress.history(room=R, task_id=T, since_id=None, limit=300)
    return [e for e in got if e["kind"] == kind]


async def kinds() -> list[str]:
    return [e["kind"] for e in await task_progress.history(room=R, task_id=T, since_id=None,
                                                           limit=300)]


async def ask(tok: int) -> int:
    return (await questions.ask(room=R, task_id=T, agent="claude", fencing_token=tok,
                                question="что делать?"))["qid"]


async def do_recover(tok: int, **over) -> dict:
    return await recovery.recover(room=R, task_id=T, expected_fencing_token=tok,
                                  **{**PROOF, **over})


async def qrow(qid: int) -> RoomTaskQuestionRow:
    async with get_session() as s:
        q = await s.get(RoomTaskQuestionRow, qid)
        s.expunge(q)
        return q


# ---- recover: G3 / G4 -------------------------------------------------------------------

async def test_g3_recover_changes_only_ownership_and_keeps_owner_block(sqlite_db):
    tok = (await claim())["fencing_token"]
    await wait(room=R, task_id=T, agent="claude", fencing_token=tok, until_seconds=3600,
               reason="жду", decision_owner="owner")
    await task_progress.set_state(room=R, task_id=T, agent="claude", fencing_token=tok,
                                  state="blocked", reason="b")
    await owner_actions.pause_by_owner(room=R, task_id=T, reason="r")
    qid = await ask(tok)
    async with get_session() as s:
        r = await s.get(RoomTaskRow, (R, T))
        r.depends_on, r.auto_pickup, r.pending_handoff_to = ["X"], True, "codex"
    before = await row()
    await expire()
    got = await do_recover(tok)
    after = await row()
    assert (after.fencing_token, after.recovery_hold) == (tok + 1, True)
    assert (after.lease_holder, after.lease_until, after.holder_session, after.holder_account,
            after.pending_handoff_to) == (None,) * 5
    for field in ("status", "owner", "waiting_until", "waiting_reason",
                  "waiting_decision_owner", "depends_on", "auto_pickup", "priority",
                  "next_action", "refs", "note"):
        assert getattr(after, field) == getattr(before, field), field
    assert (await qrow(qid)).status == "open"
    assert got["recovery_hold"] is True
    [ev] = await events("recovered")
    assert ev["fencing_token"] == tok + 1 and ev["agent"] == "owner"
    assert ev["data"]["source"] == "manual_override" and ev["data"]["proof"] == "owner_attested"
    assert ev["data"]["external_effects_unverified"] is True
    assert ev["data"]["prior"] == {"holder": "claude", "fencing_token": tok,
                                   "pending_handoff_to": "codex"}
    assert ev["data"]["run_ref"] == "sess-1"


@pytest.mark.parametrize(("over", "msg"), [
    ({"residual_risk_ack": False}, "residual_risk_ack"),
    ({"stop_source": "nonsense"}, "stop_source"),
    ({"stop_source": "other"}, "stop_ref is required"),
    ({"effects": "  "}, "effects"),
])
async def test_g4_recover_field_validation_is_422_class(sqlite_db, over, msg):
    tok = (await claim())["fencing_token"]
    await expire()
    with pytest.raises(ValueError, match=msg):
        await do_recover(tok, **over)
    assert (await row()).recovery_hold is False and await events("recovered") == []


async def test_g4_unknown_run_with_other_source_has_no_button(sqlite_db):
    tok = (await claim(session=None))["fencing_token"]
    await expire()
    with pytest.raises(ValueError, match="no proof"):
        await do_recover(tok, stop_source="other", stop_ref="я посмотрел")
    await do_recover(tok, stop_source="other", stop_ref="я посмотрел", run_ref="laptop-7")
    assert (await row()).recovery_hold is True


async def test_g4_recover_stale_fencing_and_state_refusals(sqlite_db):
    tok = (await claim())["fencing_token"]
    with pytest.raises(recovery.RecoverRefused, match="held"):  # аренда жива
        await do_recover(tok)
    await expire()
    with pytest.raises(tasks.StaleLease):
        await do_recover(tok + 5)
    await do_recover(tok)
    with pytest.raises(tasks.StaleLease):  # повтор со старым токеном
        await do_recover(tok)
    with pytest.raises(recovery.RecoverRefused, match="not_recoverable"):
        await do_recover(tok + 1)
    with pytest.raises(tasks.TaskNotFound):
        await recovery.recover(room=R, task_id="nope", expected_fencing_token=0, **PROOF)


async def test_recover_refuses_final_task(sqlite_db):
    tok = (await claim())["fencing_token"]
    await tasks.release(room=R, task_id=T, agent="claude", fencing_token=tok, status="done")
    with pytest.raises(recovery.RecoverRefused, match="final"):
        await do_recover(tok)


# ---- start: G5 / ST20 / ST10 -----------------------------------------------------------

async def recovered(*, with_question: bool = False, answer: bool = False) -> tuple[int, int | None]:
    tok = (await claim())["fencing_token"]
    qid = await ask(tok) if with_question else None
    if answer:
        await questions.answer(room=R, task_id=T, qid=qid, text="делай так")
    await expire()
    await do_recover(tok)
    return tok + 1, qid


async def start(tok: int, **kw) -> dict:
    return await recovery.start(room=R, task_id=T, expected_fencing_token=tok, **kw)


async def test_g5_start_changes_only_what_is_marked(sqlite_db):
    tok, _ = await recovered()
    async with get_session() as s:
        r = await s.get(RoomTaskRow, (R, T))
        r.waiting_until, r.waiting_reason = utc_naive_now() + timedelta(hours=1), "ci"
    before = await row()
    await start(tok)  # keep / без галки / keep
    after = await row()
    assert after.recovery_hold is False
    for field in ("status", "waiting_until", "waiting_reason", "auto_pickup", "fencing_token"):
        assert getattr(after, field) == getattr(before, field), field
    [ev] = await events("start_approved")
    assert ev["data"]["changes"] == {} and "wait_resolved" not in await kinds()


async def test_start_open_clear_wait_and_auto_pickup_are_recorded(sqlite_db):
    tok, _ = await recovered()
    async with get_session() as s:
        r = await s.get(RoomTaskRow, (R, T))
        r.waiting_until, r.waiting_decision_owner = utc_naive_now(), "owner"
    got = await start(tok, status="open", clear_wait=True, auto_pickup="on", reason="ok")
    assert (got["status"], got["auto_pickup"], got["waiting_until"]) == ("open", True, None)
    assert got["waiting_decision_owner"] is None
    [w] = await events("wait_resolved")
    assert w["data"]["by"] == "owner" and w["data"]["via"] == "start"
    [ev] = await events("start_approved")
    assert ev["data"]["changes"]["status"] == {"from": "in_progress", "to": "open"}
    assert ev["data"]["changes"]["auto_pickup"] == {"from": False, "to": True}
    assert ev["text"] == "ok"


@pytest.mark.parametrize("auto", ["on", "off"])
async def test_start_auto_pickup_flag_both_ways(sqlite_db, auto):
    tok, _ = await recovered()
    assert (await start(tok, auto_pickup=auto))["auto_pickup"] is (auto == "on")


async def test_start_requires_hold_fresh_fencing_and_valid_modes(sqlite_db):
    tok = (await claim())["fencing_token"]
    with pytest.raises(recovery.RecoverRefused, match="not_in_recovery_hold"):
        await start(tok)
    await expire()
    await do_recover(tok)
    with pytest.raises(tasks.StaleLease):
        await start(tok)
    with pytest.raises(ValueError, match="keep"):
        await start(tok + 1, status="done")
    assert (await row()).recovery_hold is True  # отказы ничего не сняли
    await start(tok + 1)
    with pytest.raises(recovery.RecoverRefused, match="not_in_recovery_hold"):
        await start(tok + 1)


async def test_st20_start_open_with_answered_question_is_422_keep_passes(sqlite_db):
    tok, _ = await recovered(with_question=True, answer=True)
    with pytest.raises(recovery.UnresolvedQuestion, match="unresolved_question"):
        await start(tok, status="open")
    after = await row()
    assert (after.status, after.recovery_hold) == ("blocked", True)
    assert (await start(tok, status="keep"))["status"] == "blocked"


async def test_start_open_with_open_question_is_also_refused(sqlite_db):
    tok, _ = await recovered(with_question=True)
    with pytest.raises(recovery.UnresolvedQuestion):
        await start(tok, status="open")


# ---- ST13 / ST10: вопрос после смерти держателя ---------------------------------------------

async def test_st13_answer_survives_recover_and_new_holder_acks_inherited(sqlite_db):
    tok, qid = await recovered(with_question=True, answer=True)
    with pytest.raises(tasks.TaskBusy, match="recovery_hold"):
        await claim("codex")
    await start(tok)
    got = await claim("codex", session="sess-2")
    assert got["status"] == "blocked"
    assert got["answered_questions"] == [{"qid": qid, "question": "что делать?",
                                          "answers": ["делай так"]}]
    await questions.ack(room=R, task_id=T, qid=qid, agent="codex",
                        fencing_token=got["fencing_token"])
    [ack] = await events("ack_answer")
    assert ack["data"] == {"qid": qid, "inherited": True, "asked_by": "claude"}
    assert (await qrow(qid)).status == "acked"
    after = await row()
    assert (after.status, after.owner) == ("in_progress", None)
    seq = [k for k in await kinds() if k in {"question", "answered", "recovered",
                                              "start_approved", "claimed", "ack_answer"}]
    assert seq == ["claimed", "question", "answered", "recovered", "start_approved",
                   "claimed", "ack_answer"]
    async with get_session() as s:
        answers = (await s.execute(select(RoomTaskAnswerRow))).scalars().all()
        assert [a.text for a in answers] == ["делай так"]


async def test_non_inherited_ack_has_plain_data(sqlite_db):
    tok = (await claim())["fencing_token"]
    qid = await ask(tok)
    await questions.answer(room=R, task_id=T, qid=qid, text="да")
    await questions.ack(room=R, task_id=T, qid=qid, agent="claude", fencing_token=tok)
    [ack] = await events("ack_answer")
    assert ack["data"] == {"qid": qid}


async def test_st10_open_question_keeps_blocking_claim_after_start(sqlite_db):
    tok, qid = await recovered(with_question=True)
    await start(tok)
    with pytest.raises(tasks.TaskBusy, match="owner_question"):
        await claim("codex")
    await questions.answer(room=R, task_id=T, qid=qid, text="да")
    assert (await claim("codex"))["answered_questions"][0]["qid"] == qid


async def test_st22_two_questions_need_every_ack(sqlite_db):
    tok = (await claim())["fencing_token"]
    q1 = await ask(tok)
    q2 = await ask(tok)
    await questions.answer(room=R, task_id=T, qid=q2, text="a2")
    await expire()
    with pytest.raises(tasks.TaskBusy, match="owner_question"):  # q1 open, q2 answered
        await claim("codex")
    await questions.answer(room=R, task_id=T, qid=q1, text="a1")
    got = await claim("codex")
    assert {a["qid"] for a in got["answered_questions"]} == {q1, q2}
    assert got["status"] == "blocked"
    new = got["fencing_token"]
    await questions.ack(room=R, task_id=T, qid=q1, agent="codex", fencing_token=new)
    with pytest.raises(OwnerBlock):  # q2 не подтверждён
        await task_progress.set_state(room=R, task_id=T, agent="codex", fencing_token=new,
                                      state="unblocked", reason="x")
    await questions.ack(room=R, task_id=T, qid=q2, agent="codex", fencing_token=new)
    assert (await row()).status == "in_progress"


# ---- закрытие вопроса владельцем: ST14 / ST15 --------------------------------------------

async def close(qid: int, tok: int, reason: str = "держатель умер") -> dict:
    return await owner_actions.close_question_by_owner(
        room=R, task_id=T, qid=qid, reason=reason, expected_fencing_token=tok)


async def test_st14_close_open_question_then_start_claim_unblock(sqlite_db):
    tok, qid = await recovered(with_question=True)
    got = await close(qid, tok)
    assert got["changed"] is True and got["question"]["status"] == "withdrawn"
    q = await qrow(qid)
    assert (q.status, q.ack_by, q.question) == ("withdrawn", "owner", "что делать?")
    assert (await row()).status == "blocked"  # S7: статус не меняется
    [ev] = await events("question_closed")
    assert ev["text"] == "держатель умер" and ev["data"]["qid"] == qid
    assert ev["data"]["was"] == "open" and ev["data"]["by"] == "owner"
    await start(tok)
    new = (await claim("codex"))["fencing_token"]
    assert (await task_progress.set_state(room=R, task_id=T, agent="codex", fencing_token=new,
                                          state="unblocked", reason="ok"))["status"] == "in_progress"


async def test_st14_close_requires_reason(sqlite_db):
    tok, qid = await recovered(with_question=True)
    for bad in ("", "  ", "x" * 2001):
        with pytest.raises(ValueError):
            await close(qid, tok, bad)
    assert (await qrow(qid)).status == "open"


async def test_st15_close_refuses_live_holder_stale_fencing_and_repeats_silently(sqlite_db):
    tok = (await claim())["fencing_token"]
    qid = await ask(tok)
    with pytest.raises(tasks.TaskBusy, match="held by claude"):
        await close(qid, tok)
    await expire()
    with pytest.raises(tasks.StaleLease):
        await close(qid, tok + 3)
    assert (await qrow(qid)).status == "open"
    assert (await close(qid, tok))["changed"] is True
    again = await close(qid, tok)
    assert again["changed"] is False
    assert len(await events("question_closed")) == 1


async def test_close_answered_question_keeps_answers_and_missing_or_acked_refused(sqlite_db):
    tok, qid = await recovered(with_question=True, answer=True)
    got = await close(qid, tok)
    assert [a["text"] for a in got["question"]["answers"]] == ["делай так"]
    with pytest.raises(questions.QuestionNotFound):
        await close(9999, tok)


async def test_close_acked_question_is_refused(sqlite_db):
    tok = (await claim())["fencing_token"]
    qid = await ask(tok)
    await questions.answer(room=R, task_id=T, qid=qid, text="да")
    await questions.ack(room=R, task_id=T, qid=qid, agent="claude", fencing_token=tok)
    await expire()
    with pytest.raises(questions.QuestionState):
        await close(qid, tok)


# ---- wait/resolve: ST9 ------------------------------------------------------------------

async def test_st9_wait_resolve_clears_owner_wait_only_and_is_idempotent(sqlite_db):
    tok = (await claim())["fencing_token"]
    await wait(room=R, task_id=T, agent="claude", fencing_token=tok, until_seconds=3600,
               reason="жду решения", decision_owner="owner")
    await task_progress.set_state(room=R, task_id=T, agent="claude", fencing_token=tok,
                                  state="blocked", reason="b")
    got = await owner_actions.resolve_wait_by_owner(room=R, task_id=T, reason="решил")
    assert got["changed"] is True and got["waiting_until"] is None
    assert got["status"] == "blocked" and got["waiting_decision_owner"] is None
    [ev] = await events("wait_resolved")
    assert ev["data"]["by"] == "owner" and ev["data"]["was"]["decision_owner"] == "owner"
    assert ev["data"]["was"]["reason"] == "жду решения"
    assert (await owner_actions.resolve_wait_by_owner(
        room=R, task_id=T, reason="ещё"))["changed"] is False
    assert len(await events("wait_resolved")) == 1


async def test_wait_resolve_leaves_holder_wait_and_validates(sqlite_db):
    tok = (await claim())["fencing_token"]
    await wait(room=R, task_id=T, agent="claude", fencing_token=tok, until_seconds=3600,
               reason="CI")
    got = await owner_actions.resolve_wait_by_owner(room=R, task_id=T, reason="r")
    assert got["changed"] is False and got["waiting_reason"] == "CI"
    with pytest.raises(ValueError):
        await owner_actions.resolve_wait_by_owner(room=R, task_id=T, reason=" ")
    with pytest.raises(tasks.TaskNotFound):
        await owner_actions.resolve_wait_by_owner(room=R, task_id="nope", reason="r")
    await tasks.release(room=R, task_id=T, agent="claude", fencing_token=tok, status="done")
    with pytest.raises(tasks.TaskBusy):
        await owner_actions.resolve_wait_by_owner(room=R, task_id=T, reason="r")


async def test_wait_resolve_lets_claim_pass_owner_wait_gate(sqlite_db):
    tok = (await claim())["fencing_token"]
    await wait(room=R, task_id=T, agent="claude", fencing_token=tok, until_seconds=3600,
               reason="жду", decision_owner="owner")
    await expire()
    with pytest.raises(tasks.TaskBusy, match="owner_wait"):
        await claim("codex")
    await owner_actions.resolve_wait_by_owner(room=R, task_id=T, reason="r")
    assert (await claim("codex"))["lease_holder"] == "codex"
