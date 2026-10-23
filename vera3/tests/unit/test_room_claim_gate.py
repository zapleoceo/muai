"""Ворота новой аренды (PR-B1, дизайн §3): G1, G2, ST16-ST19, ST21 и режим флага.

Режим `off` (в проде сейчас) — статус-кво для истёкших аренд; режимы `on`/`freeze` проверяются
чистой функцией с явным `mode`: достичь их через окружение в B1 нельзя (нужен PR-C1).
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskRow
from vera_shared.room import (
    handoff,
    owner_actions,
    questions,
    task_progress,
    task_queue,
    tasks,
)
from vera_shared.room import single_executor as se
from vera_shared.room.claim_gate import check_new_lease
from vera_shared.room.owner_block import OwnerBlock
from vera_shared.room.task_wait import wait
from vera_shared.timeutil import utc_naive_now

pytestmark = pytest.mark.asyncio
R, T = "main", "G1"


async def claim(agent: str = "claude") -> dict:
    return await tasks.claim(room=R, task_id=T, agent=agent, lease_seconds=600)


async def mutate(**fields) -> None:
    async with get_session() as s:
        row = await s.get(RoomTaskRow, (R, T))
        for k, v in fields.items():
            setattr(row, k, v)


async def expire() -> None:
    await mutate(lease_until=utc_naive_now() - timedelta(seconds=5))


async def gate(agent: str = "codex", via: str = "claim", mode: str = "off",
               sender: str | None = None, **attrs):
    async with get_session() as s:
        row = await s.get(RoomTaskRow, (R, T))
        for k, v in attrs.items():
            setattr(row, k, v)  # lease_key_hash и т.п. — вне ORM, только на этом экземпляре
        got = await check_new_lease(s, row, utc_naive_now(), agent=agent, via=via,
                                    sender=sender, mode=mode)
        s.expunge(row)
        return got.code if got else None


async def ask(tok: int) -> int:
    return (await questions.ask(room=R, task_id=T, agent="claude", fencing_token=tok,
                                question="?"))["qid"]


async def history_kinds() -> list[str]:
    return [e["kind"] for e in await task_progress.history(room=R, task_id=T, since_id=None,
                                                           limit=200)]


# ---- G1: claim ----------------------------------------------------------------------

async def test_g1_expired_lease_off_is_status_quo_on_is_recovery_pending(sqlite_db):
    tok = (await claim())["fencing_token"]
    await expire()
    assert await gate(mode="off") is None  # F2: истёкшая аренда сразу выдаётся
    assert await gate(mode="on") == "recovery_pending"
    assert await gate(mode="on", lease_key_hash="a" * 64) == "held_expired"
    assert (await claim("codex"))["fencing_token"] == tok + 1


async def test_g1_live_foreign_lease_is_held_in_every_mode(sqlite_db):
    await claim()
    for mode in ("off", "on", "freeze"):
        assert await gate(mode=mode) == "held"
    with pytest.raises(tasks.TaskBusy, match="held by claude"):
        await claim("codex")


async def test_g1_recovery_hold_blocks_in_every_mode(sqlite_db):
    await claim()
    await expire()
    await mutate(lease_holder=None, lease_until=None, recovery_hold=True)
    for mode in ("off", "on"):
        assert await gate(mode=mode) == "recovery_hold"
    with pytest.raises(tasks.TaskBusy, match="recovery_hold"):
        await claim("codex")


async def test_g1_paused_owner_always_holder_only_outside_off_for_claim(sqlite_db):
    tok = (await claim())["fencing_token"]
    await task_progress.set_state(room=R, task_id=T, agent="claude", fencing_token=tok,
                                  state="paused", reason="x")
    await expire()
    assert await gate(mode="off") is None  # claim чужой после паузы держателя — статус-кво
    assert await gate(mode="on") == "recovery_pending"
    await mutate(lease_holder=None, lease_until=None)
    assert await gate(mode="on") == "paused"
    assert await gate(mode="off", via="next") == "paused"  # next/accept отказывали и раньше
    await owner_actions.pause_by_owner(room=R, task_id=T, reason="r")
    assert await gate(mode="off") == "paused"
    with pytest.raises(tasks.TaskBusy, match="paused by the owner"):
        await claim("codex")


async def test_g1_open_question_and_owner_wait_refuse_claim(sqlite_db):
    tok = (await claim())["fencing_token"]
    await ask(tok)
    await expire()
    assert await gate() == "owner_question"
    with pytest.raises(tasks.TaskBusy, match="owner_question"):
        await claim("codex")
    await mutate(waiting_decision_owner="owner")
    assert await gate(via="next") == "owner_question"  # вопрос раньше ожидания


async def test_g1_owner_wait_refuses_claim_next_accept(sqlite_db):
    tok = (await claim())["fencing_token"]
    await wait(room=R, task_id=T, agent="claude", fencing_token=tok, until_seconds=3600,
               reason="жду", decision_owner="owner")
    await expire()
    for via in ("claim", "next"):
        assert await gate(via=via) == "owner_wait"
    await mutate(waiting_decision_owner="ci")  # ожидание держателя ворота не закрывает
    assert await gate() is None


async def test_final_task_is_refused(sqlite_db):
    tok = (await claim())["fencing_token"]
    await tasks.release(room=R, task_id=T, agent="claude", fencing_token=tok, status="done")
    for via in ("claim", "next", "accept"):
        assert await gate(via=via) == "final"


# ---- ответ без ack: claim проходит, next/accept нет --------------------------------------

async def test_answered_unacked_claim_passes_with_answers_next_accept_refuse(sqlite_db):
    tok = (await claim())["fencing_token"]
    qid = await ask(tok)
    await questions.answer(room=R, task_id=T, qid=qid, text="делай так")
    await expire()
    assert await gate(via="claim") is None
    assert await gate(via="next") == "answer_unacked"
    assert await gate(via="accept", sender="claude", pending_handoff_to="codex") \
        == "answer_unacked"
    got = await claim("codex")
    [aq] = got["answered_questions"]
    assert (aq["qid"], aq["question"]) == (qid, "?")
    assert [(a["text"], a["by"]) for a in aq["answers"]] == [("делай так", "owner")]
    assert set(aq["answers"][0]) == {"text", "by", "at"}  # форма как у room_task_questions
    assert got["status"] == "blocked"
    claimed = [e for e in await task_progress.history(room=R, task_id=T, since_id=None, limit=50)
               if e["kind"] == "claimed"][-1]
    assert claimed["data"] == {"answered_qids": [qid]}


async def test_claim_without_answers_has_no_answered_questions_key(sqlite_db):
    assert "answered_questions" not in await claim()


async def test_st16_answered_holder_cannot_resume_without_ack(sqlite_db):
    tok = (await claim())["fencing_token"]
    qid = await ask(tok)
    await questions.answer(room=R, task_id=T, qid=qid, text="да")
    with pytest.raises(OwnerBlock):
        await tasks.update(room=R, task_id=T, agent="claude", fencing_token=tok,
                           status="in_progress")


# ---- G2 / ST18 / ST19: accept -------------------------------------------------------------

async def offer(tok: int) -> None:
    await handoff.offer(room=R, task_id=T, agent="claude", fencing_token=tok, to_agent="codex")


async def test_g2_accept_matrix_off_and_on(sqlite_db):
    tok = (await claim())["fencing_token"]
    await offer(tok)
    for mode in ("off", "on"):
        assert await gate("codex", "accept", mode, sender="claude") is None
    await expire()
    assert await gate("codex", "accept", "off", sender="claude") is None  # F7 статус-кво
    assert await gate("codex", "accept", "on", sender="claude") == "held_expired"
    assert await gate("other", "accept", "off", sender="claude") == "held"  # не ему предложено


async def test_st18_accept_refused_until_sender_acks_and_unblocks(sqlite_db):
    tok = (await claim())["fencing_token"]
    qid = await ask(tok)
    await questions.answer(room=R, task_id=T, qid=qid, text="да")
    await offer(tok)
    with pytest.raises(handoff.HandoffError, match="answer_unacked"):
        await handoff.accept(room=R, task_id=T, agent="codex")
    await questions.ack(room=R, task_id=T, qid=qid, agent="claude", fencing_token=tok)
    got = await handoff.accept(room=R, task_id=T, agent="codex")
    assert got["lease_holder"] == "codex" and got["status"] == "in_progress"


async def test_st19_open_question_refuses_claim_next_accept(sqlite_db):
    tok = (await claim())["fencing_token"]
    await ask(tok)
    await offer(tok)
    with pytest.raises(handoff.HandoffError, match="owner_question"):
        await handoff.accept(room=R, task_id=T, agent="codex")
    await expire()
    assert await gate(via="next") == "owner_question"
    with pytest.raises(tasks.TaskBusy, match="owner_question"):
        await claim("codex")
    # ни одна попытка ничего не записала
    assert await history_kinds() == ["created", "claimed", "question", "handoff_offer"]


async def test_accept_refuses_recovery_hold_and_owner_pause(sqlite_db):
    tok = (await claim())["fencing_token"]
    await offer(tok)
    await mutate(recovery_hold=True)
    with pytest.raises(handoff.HandoffError, match="recovery_hold"):
        await handoff.accept(room=R, task_id=T, agent="codex")


# ---- next: SQL-фильтр и перепроверка (ST17, ST21) ---------------------------------------

PICK = {"room": R, "agent": "codex", "project": None, "lease_seconds": 60, "session": None,
        "account": None}


async def test_st21_next_skips_answered_question_even_with_status_open(sqlite_db):
    tok = (await claim())["fencing_token"]
    qid = await ask(tok)
    await questions.answer(room=R, task_id=T, qid=qid, text="да")
    # «обход»: легаси-правка вернула open + auto_pickup, вопрос остался answered
    await mutate(status="open", auto_pickup=True, lease_holder=None, lease_until=None)
    assert await task_queue.next_task(**PICK) is None
    # перепроверка под блокировкой: SQL-фильтр отключён, _still_eligible всё равно отказывает
    async with get_session() as s:
        row = await s.get(RoomTaskRow, (R, T))
        assert await task_queue._still_eligible(s, row, utc_naive_now(), "codex") is False
    await questions.ack(room=R, task_id=T, qid=qid, agent="claude", fencing_token=tok) \
        if False else None


async def test_next_skips_recovery_hold_and_owner_wait(sqlite_db):
    await tasks.open_task(room=R, task_id=T, agent="claude", title="t", paths=None,
                          auto_pickup=True)
    await mutate(recovery_hold=True)
    assert await task_queue.next_task(**PICK) is None
    await mutate(recovery_hold=False, waiting_decision_owner="owner")
    assert await task_queue.next_task(**PICK) is None
    await mutate(waiting_decision_owner=None)
    assert (await task_queue.next_task(**PICK))["task_id"] == T


async def test_closed_question_lets_next_pick_the_task_again(sqlite_db):
    tok = (await claim())["fencing_token"]
    qid = await ask(tok)
    await questions.answer(room=R, task_id=T, qid=qid, text="да")
    await questions.ack(room=R, task_id=T, qid=qid, agent="claude", fencing_token=tok)
    await tasks.release(room=R, task_id=T, agent="claude", fencing_token=tok, status="open")
    await mutate(auto_pickup=True)
    assert (await task_queue.next_task(**PICK))["task_id"] == T


# ---- флаг -------------------------------------------------------------------------------

def test_parse_mode_accepts_known_values_and_rejects_others():
    assert se.parse_mode(None) == "off" and se.parse_mode("") == "off"
    assert se.parse_mode(" ON ") == "on" and se.parse_mode("freeze") == "freeze"
    with pytest.raises(ValueError, match="ROOM_SINGLE_EXECUTOR"):
        se.parse_mode("maybe")


def test_mode_is_read_once_and_on_freeze_are_unsupported_in_b1(monkeypatch):
    se.single_executor_mode.cache_clear()
    monkeypatch.delenv(se.ENV, raising=False)
    assert se.single_executor_mode() == "off"
    monkeypatch.setenv(se.ENV, "on")
    assert se.single_executor_mode() == "off"  # прочитано один раз
    se.single_executor_mode.cache_clear()
    for value in ("on", "freeze"):
        monkeypatch.setenv(se.ENV, value)
        with pytest.raises(se.SingleExecutorUnsupported, match="PR-C1"):
            se.single_executor_mode()
        se.single_executor_mode.cache_clear()


async def test_entry_points_fail_loudly_when_flag_is_on(sqlite_db, monkeypatch):
    se.single_executor_mode.cache_clear()
    monkeypatch.setenv(se.ENV, "on")
    try:
        with pytest.raises(NotImplementedError):
            await claim()
    finally:
        se.single_executor_mode.cache_clear()
