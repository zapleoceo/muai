"""Срочная задача ждёт исполнителя: очередь не done до claim, удержание, получатель dot.

Закрепляем: задача в `room_task_next` (auto_pickup); строка очереди
`status=opened`, пока задачу не взяли, и закрывается на claim. Неподтверждённый
источник — один вопрос владельцу (повтор и рестарт его не дублируют), задача
не выдаётся до ответа; ответ владельца — ref `source_confirmed` и обычный
next_action, ответ не владельца удержание не снимает. Поручение про dot —
получатель результата dot, ответственный прежний.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from bot_telegram import help_worker, voice_worker
from gateway.voice_command import accept_voice_command
from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskRow
from vera_shared.room import questions, tasks
from vera_shared.room.task_queue import next_task
from vera_shared.room.tasks import TaskBusy
from vera_shared.timeutil import utc_naive_now
from vera_shared.voice_help.hold import ask_owner_once
from vera_shared.voice_help.policy import (
    CONFIRMED_NOTE,
    DOT_RESULT_NOTE,
    RESULT_RECIPIENT_REF,
    SOURCE_CONFIRMED,
    UNCERTAIN_HOLD,
    routes_to_dot,
)
from voice_help_kit import (
    OWNER,
    ROOM,
    SECRET,
    Recorder,
    answer,
    help_cmd,
    queue_row,
    tasks_in,
)


def uses_db(test):
    return pytest.mark.asyncio(pytest.mark.usefixtures("sqlite_db")(test))


@pytest.fixture(autouse=True)
def _test_room(monkeypatch):
    monkeypatch.setenv("VOICE_HELP_ROOM", ROOM)


async def _open(command_id: str = "vc-h", **kw) -> Recorder:
    await accept_voice_command(help_cmd(command_id, **kw), x_internal_secret=SECRET)
    rec = Recorder()
    with patch.object(voice_worker, "ask_brain", AsyncMock(return_value=answer())), \
         patch.object(voice_worker, "save_event", AsyncMock()):
        assert await voice_worker.process_one(rec, OWNER)
        assert not await voice_worker.process_one(rec, OWNER)
    return rec


async def _pick(agent: str = "claude"):
    return await next_task(room=ROOM, agent=agent, project=None, lease_seconds=900,
                           session=None, account=None)


@uses_db
async def test_queue_row_waits_for_claim_then_closes():
    rec = await _open()
    row = await queue_row("vc-h")
    assert (row.status, row.help_state) == ("opened", "opened") and row.instruction
    now = row.task_opened_at
    assert await help_worker.track_help(rec, now + timedelta(minutes=1)) == 0
    assert (await queue_row("vc-h")).status == "opened"
    picked = await _pick()
    assert picked is not None and picked["task_id"] == "help-vc-h"
    assert await help_worker.track_help(rec, now + timedelta(minutes=2)) == 1
    assert rec.sent[-1] == help_worker.taken_text("claude")
    row = await queue_row("vc-h")
    assert (row.status, row.help_state, row.instruction) == ("done", "taken", "")


@uses_db
async def test_uncertain_holds_until_owner_answers():
    rec = await _open(doubts=["blind"])
    [task] = await tasks_in(ROOM)
    assert UNCERTAIN_HOLD in task.next_action
    assert task.auto_pickup is False
    [q] = await questions.questions_of(ROOM, "help-vc-h")
    assert q["status"] == "open"
    assert not await ask_owner_once(ROOM, "help-vc-h")
    assert len(await questions.questions_of(ROOM, "help-vc-h")) == 1
    assert await _pick() is None
    now = (await queue_row("vc-h")).task_opened_at
    await questions.answer(room=ROOM, task_id="help-vc-h", qid=q["qid"], text="да",
                           by="codex")
    await help_worker.track_help(rec, now + timedelta(seconds=30))
    [task] = await tasks_in(ROOM)
    assert SOURCE_CONFIRMED not in [r["ref"] for r in task.refs]
    await questions.answer(room=ROOM, task_id="help-vc-h", qid=q["qid"], text="да, я")
    await help_worker.track_help(rec, now + timedelta(seconds=40))
    await help_worker.track_help(rec, now + timedelta(seconds=50))
    [task] = await tasks_in(ROOM)
    assert [r["ref"] for r in task.refs].count(SOURCE_CONFIRMED) == 1
    assert task.next_action.startswith(CONFIRMED_NOTE)
    assert UNCERTAIN_HOLD not in task.next_action
    assert task.auto_pickup is True
    # B1: ответ обработан — вопрос бота закрыт, иначе answered без ack выводит задачу из next
    [q] = await questions.questions_of(ROOM, "help-vc-h")
    assert (q["status"], q["ack_by"]) == ("acked", "vera")
    assert (await _pick())["task_id"] == "help-vc-h"


@uses_db
async def test_direct_claim_waits_for_owner_decision():
    rec = await _open(doubts=["blind"])
    [q] = await questions.questions_of(ROOM, "help-vc-h")
    with pytest.raises(TaskBusy, match="unconfirmed voice source"):
        await tasks.claim(room=ROOM, task_id="help-vc-h", agent="codex",
                          lease_seconds=900)
    await questions.answer(room=ROOM, task_id="help-vc-h", qid=q["qid"],
                           text="может быть")
    assert await _pick() is None
    with pytest.raises(TaskBusy, match="unconfirmed voice source"):
        await tasks.claim(room=ROOM, task_id="help-vc-h", agent="codex",
                          lease_seconds=900)
    [task] = await tasks_in(ROOM)
    assert task.auto_pickup is False
    assert SOURCE_CONFIRMED not in [r["ref"] for r in task.refs]
    now = (await queue_row("vc-h")).task_opened_at
    assert await help_worker.track_help(rec, now + timedelta(seconds=30)) == 0


@uses_db
async def test_legacy_auto_pickup_stays_held_after_ambiguous_answer():
    rec = await _open(doubts=["blind"])
    async with get_session() as session:
        row = await session.get(RoomTaskRow, (ROOM, "help-vc-h"))
        row.auto_pickup = True  # задача, созданная до исправления
    [q] = await questions.questions_of(ROOM, "help-vc-h")
    await questions.answer(room=ROOM, task_id="help-vc-h", qid=q["qid"],
                           text="не уверен")
    assert await _pick() is None
    now = (await queue_row("vc-h")).task_opened_at
    assert await help_worker.track_help(rec, now + timedelta(seconds=30)) == 0
    with pytest.raises(TaskBusy, match="unconfirmed voice source"):
        await tasks.claim(room=ROOM, task_id="help-vc-h", agent="codex",
                          lease_seconds=900)


@uses_db
async def test_legacy_holder_cannot_enable_unverified_auto_pickup():
    await _open(doubts=["blind"])
    async with get_session() as session:
        row = await session.get(RoomTaskRow, (ROOM, "help-vc-h"), with_for_update=True)
        tasks.apply_claim(row, agent="codex", now=utc_naive_now(), lease_seconds=60,
                          session=None, account=None)
        token = row.fencing_token
    with pytest.raises(TaskBusy, match="cannot remove hold"):
        await tasks.update(room=ROOM, task_id="help-vc-h", agent="codex",
                           fencing_token=token, auto_pickup=True)
    [task] = await tasks_in(ROOM)
    assert task.auto_pickup is False
    with pytest.raises(TaskBusy, match="cannot remove hold"):
        await tasks.update(room=ROOM, task_id="help-vc-h", agent="codex",
                           fencing_token=token, refs=[])
    with pytest.raises(TaskBusy, match="cannot remove hold"):
        await tasks.update(room=ROOM, task_id="help-vc-h", agent="codex",
                           fencing_token=token,
                           refs=[*task.refs, {"kind": "source", "ref": SOURCE_CONFIRMED}])


@uses_db
async def test_owner_denial_cancels_without_claim():
    rec = await _open(doubts=["blind"])
    [q] = await questions.questions_of(ROOM, "help-vc-h")
    await questions.answer(room=ROOM, task_id="help-vc-h", qid=q["qid"],
                           text="нет, это не я")
    now = (await queue_row("vc-h")).task_opened_at
    assert await help_worker.track_help(rec, now + timedelta(seconds=30)) == 1
    [task] = await tasks_in(ROOM)
    assert task.status == "cancelled"
    assert task.auto_pickup is False
    assert SOURCE_CONFIRMED not in [r["ref"] for r in task.refs]
    assert await _pick() is None
    with pytest.raises(TaskBusy, match="cancelled"):
        await tasks.claim(room=ROOM, task_id="help-vc-h", agent="codex",
                          lease_seconds=900)
    assert (await queue_row("vc-h")).status == "done"


@uses_db
async def test_owner_answer_lifts_then_claims():
    rec = await _open(doubts=["blind"])
    [q] = await questions.questions_of(ROOM, "help-vc-h")
    await questions.answer(room=ROOM, task_id="help-vc-h", qid=q["qid"], text="да")
    with pytest.raises(TaskBusy, match="unconfirmed voice source"):
        await tasks.claim(room=ROOM, task_id="help-vc-h", agent="codex",
                          lease_seconds=900)
    now = (await queue_row("vc-h")).task_opened_at
    assert await help_worker.track_help(rec, now + timedelta(seconds=30)) == 0
    await tasks.claim(room=ROOM, task_id="help-vc-h", agent="codex", lease_seconds=900)
    assert await help_worker.track_help(rec, now + timedelta(minutes=1)) == 1
    [task] = await tasks_in(ROOM)
    assert SOURCE_CONFIRMED in [r["ref"] for r in task.refs]
    assert (await queue_row("vc-h")).status == "done"


@uses_db
async def test_dot_phrase_sets_result_recipient_not_responsible():
    await _open(instruction="подними переписку с Dot про бюджет")
    [task] = await tasks_in(ROOM)
    assert task.responsible == "Claude"
    assert DOT_RESULT_NOTE in task.next_action
    assert RESULT_RECIPIENT_REF in [r["ref"] for r in task.refs]


@pytest.mark.parametrize(("text", "dot"), [
    ("спроси у ChatGPT", True), ("напиши доцу", True), ("глянь дотс", True),
    ("переписку с DOT найди", True), ("посмотри логи бота", False),
    ("anecdote про кота", False),
])
def test_routes_to_dot(text, dot):
    assert routes_to_dot(text) is dot


@uses_db
async def test_uncertain_task_cannot_auto_pick_before_question_exists():
    from vera_shared.voice_help import room_intake

    with patch.object(room_intake, "ask_owner_once", AsyncMock()):
        await room_intake.open_help_task(
            command_id="vc-pre-question", event_id=None, instruction="посмотри логи",
            source={"guard": "blind", "doubts": ["blind"]}, uncertain=True)
    [task] = await tasks_in(ROOM)
    assert task.auto_pickup is False
    assert await _pick() is None
