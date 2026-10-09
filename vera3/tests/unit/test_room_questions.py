"""Трекер задач, шаг 3: вопросы владельцу (ask → answer → ack), attention, MCP."""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from vera_mcp import room_question_tools as qt
from vera_mcp import room_tools as r
from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskRow
from vera_shared.room import messages, questions, task_attention
from vera_shared.room.attention import ANSWERED, NEEDS_OWNER, attention
from vera_shared.room.tasks import StaleLease
from vera_shared.timeutil import utc_naive_now

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 10, 9, 12, 0)


def ctx(client: str):
    return SimpleNamespace(request_context=SimpleNamespace(
        request=SimpleNamespace(scope={"mcp_client": client})))


CLAUDE, CODEX = ctx("claude"), ctx("codex")


async def task_row(task_id: str = "T1") -> RoomTaskRow:
    async with get_session() as s:
        row = await s.get(RoomTaskRow, ("main", task_id))
        s.expunge(row)
        return row


async def kinds(task_id: str = "T1") -> list[str]:
    return [e["kind"] for e in (await r.room_task_history(task_id))["events"]]


async def held() -> int:
    return (await r.room_task_claim("T1", CLAUDE))["task"]["fencing_token"]


async def ask(tok: int, text: str = "?") -> int:
    return (await qt.room_task_ask("T1", tok, CLAUDE, text))["question"]["qid"]


async def test_ask_blocks_task_and_posts_room_message(sqlite_db):
    tok = await held()
    q = (await qt.room_task_ask("T1", tok, CLAUDE, "Какую БД?"))["question"]
    assert q["status"] == "open" and q["asked_by"] == "claude"
    row = await task_row()
    assert (row.status, row.owner) == ("blocked", "owner")
    msg = (await messages.history(room="main", limit=5))[-1]
    assert msg["status"] == "question" and msg["task_id"] == "T1" and msg["to"] is None
    assert f"#{q['qid']}" in msg["body"]
    assert (await kinds())[-1] == "question"


async def test_ask_requires_live_lease_and_current_token(sqlite_db):
    tok = await held()
    with pytest.raises(StaleLease):
        await qt.room_task_ask("T1", tok + 5, CLAUDE, "?")
    with pytest.raises(StaleLease):
        await qt.room_task_ask("T1", tok, CODEX, "?")
    assert await questions.questions_of("main", "T1") == []


async def test_full_round_trip_answer_does_not_unblock(sqlite_db):
    tok = await held()
    qid = await ask(tok, "Вопрос?")
    q, changed = await questions.answer(room="main", task_id="T1", qid=qid, text="Да")
    assert changed and q["status"] == "answered" and q["answers"][0]["by"] == "owner"
    row = await task_row()
    assert (row.status, row.owner) == ("blocked", "owner")  # ответ статус не снимает
    reply = (await messages.history(room="main", limit=5))[-1]
    assert reply["to"] == "claude" and reply["in_reply_to"] == f"task-q-{qid}"
    assert reply["from"] == "owner"
    acked = (await qt.room_task_answer_ack("T1", tok, qid, CLAUDE))["question"]
    assert acked["status"] == "acked" and acked["ack_by"] == "claude"
    row = await task_row()
    assert (row.status, row.owner) == ("in_progress", None)
    assert (await kinds())[-4:] == ["question", "answered", "ack_answer", "unblocked"]


async def test_ack_needs_lease_and_an_answer(sqlite_db):
    tok = await held()
    qid = await ask(tok)
    with pytest.raises(questions.QuestionState):
        await qt.room_task_answer_ack("T1", tok, qid, CLAUDE)  # ответа ещё нет
    await questions.answer(room="main", task_id="T1", qid=qid, text="ок")
    with pytest.raises(StaleLease):
        await qt.room_task_answer_ack("T1", tok, qid, CODEX)
    with pytest.raises(StaleLease):
        await qt.room_task_answer_ack("T1", tok + 1, qid, CLAUDE)
    assert (await task_row()).status == "blocked"


async def test_duplicate_answer_is_noop_and_edit_keeps_history(sqlite_db):
    tok = await held()
    qid = await ask(tok)
    await questions.answer(room="main", task_id="T1", qid=qid, text="Да")
    q, changed = await questions.answer(room="main", task_id="T1", qid=qid, text=" Да ")
    assert not changed and len(q["answers"]) == 1
    q, changed = await questions.answer(room="main", task_id="T1", qid=qid, text="Нет")
    assert changed and [a["text"] for a in q["answers"]] == ["Да", "Нет"]
    assert (await kinds()).count("answered") == 2


async def test_answer_rejects_foreign_closed_and_empty(sqlite_db):
    tok = await held()
    qid = await ask(tok)
    with pytest.raises(questions.QuestionNotFound):
        await questions.answer(room="main", task_id="OTHER", qid=qid, text="x")
    with pytest.raises(questions.QuestionNotFound):
        await questions.answer(room="main", task_id="T1", qid=999, text="x")
    with pytest.raises(ValueError):
        await questions.answer(room="main", task_id="T1", qid=qid, text="   ")
    await qt.room_task_answer_ack("T1", tok, qid, CLAUDE, withdraw=True)
    with pytest.raises(questions.QuestionState):
        await questions.answer(room="main", task_id="T1", qid=qid, text="поздно")


async def test_withdraw_only_by_asker_and_unblocks(sqlite_db):
    tok = await held()
    qid = await ask(tok)
    with pytest.raises(StaleLease):
        await qt.room_task_answer_ack("T1", tok, qid, CODEX, withdraw=True)
    q = (await qt.room_task_answer_ack("T1", tok, qid, CLAUDE, withdraw=True))["question"]
    assert q["status"] == "withdrawn"
    assert (await task_row()).status == "in_progress"


async def test_task_stays_blocked_while_another_question_is_open(sqlite_db):
    tok = await held()
    first = await ask(tok, "1?")
    await ask(tok, "2?")
    await questions.answer(room="main", task_id="T1", qid=first, text="ok")
    await qt.room_task_answer_ack("T1", tok, first, CLAUDE)
    assert (await task_row()).status == "blocked"
    got = (await qt.room_task_questions("T1"))["questions"]
    assert [x["status"] for x in got] == ["acked", "open"]


async def test_attention_needs_owner_then_answered_then_clear(sqlite_db):
    tok = await held()
    qid = await ask(tok)

    async def state() -> str:
        row = await task_row()
        async with get_session() as s:
            amap = await task_attention.attention_map(s, [row], utc_naive_now())
        return amap[("main", "T1")].state

    assert await state() == NEEDS_OWNER
    await questions.answer(room="main", task_id="T1", qid=qid, text="ok")
    assert await state() == ANSWERED
    await qt.room_task_answer_ack("T1", tok, qid, CLAUDE)
    assert await state() == "in_progress"


def test_attention_answered_pure():
    row = SimpleNamespace(
        status="blocked", lease_holder="claude", lease_until=NOW + timedelta(hours=1),
        last_progress_at=None, last_progress_text=None, next_checkpoint_at=None,
        waiting_until=None, updated_at=NOW)
    a = attention(row, NOW, answered_at=NOW - timedelta(minutes=5))
    assert a.state == ANSWERED and "ждёт исполнителя" in a.label_ru
    assert attention(row, NOW, open_question_at=NOW, answered_at=NOW).state == NEEDS_OWNER


def test_mcp_has_no_answer_tool():
    assert {t.__name__ for t in qt.QUESTION_TOOLS} == {
        "room_task_ask", "room_task_answer_ack", "room_task_questions"}


async def test_ask_validates_question_length(sqlite_db):
    tok = await held()
    for bad in ("   ", "x" * 4001):
        with pytest.raises(ValueError):
            await questions.ask(room="main", task_id="T1", agent="claude",
                                fencing_token=tok, question=bad)
    assert await questions.questions_of("main", "T1") == []


async def test_ack_leaves_status_alone_unless_task_is_blocked(sqlite_db):
    tok = await held()
    qid = await ask(tok)
    await questions.answer(room="main", task_id="T1", qid=qid, text="ok")
    await r.room_task_update("T1", tok, CLAUDE, status="in_progress")
    await qt.room_task_answer_ack("T1", tok, qid, CLAUDE)
    row = await task_row()
    assert (row.status, row.owner) == ("in_progress", "owner")
    assert "unblocked" not in await kinds()


async def test_answer_rejected_for_finished_task(sqlite_db):
    tok = await held()
    qid = await ask(tok)
    await r.room_task_release("T1", tok, CLAUDE, status="done")
    with pytest.raises(questions.QuestionState):
        await questions.answer(room="main", task_id="T1", qid=qid, text="поздно")
