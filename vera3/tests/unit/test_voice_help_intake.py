"""Срочная просьба голосом: от шлюза до задачи в комнате и ответа владельцу.

Закрепляем: высокая уверенность — сразу срочная задача (priority 0, без
автоподбора, ответственный Claude) и пост-request; повтор и рестарт — одна
задача; переспрос и пограничная уверенность задачу не заводят; без «Да» —
ничего; секреты в комнату не попадают; боевая `main` в тестах не трогается.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from bot_telegram import help_worker, voice_worker
from bot_telegram.help_confirm import handle_confirmation, parse_callback
from gateway.voice_command import VoiceCommand, accept_voice_command
from pydantic import ValidationError
from sqlalchemy import update
from vera_shared.db.engine import get_session
from vera_shared.db.models_voice import VoiceCommandRow
from vera_shared.redact import MASK
from vera_shared.timeutil import utc_naive_now
from vera_shared.voice_commands import claim_command
from vera_shared.voice_help.policy import SAFETY_NOTE
from vera_shared.voice_help.queue_state import answer_confirmation
from voice_help_kit import (
    INSTRUCTION,
    OWNER,
    ROOM,
    SECRET,
    Recorder,
    answer,
    help_cmd,
    messages_in,
    queue_row,
    tasks_in,
)

pytestmark = pytest.mark.asyncio
uses_db = pytest.mark.usefixtures("sqlite_db")


@pytest.fixture(autouse=True)
def _test_room(monkeypatch):
    monkeypatch.setenv("VOICE_HELP_ROOM", ROOM)


async def _run(rec: Recorder, times: int = 1) -> list[bool]:
    brain = AsyncMock(return_value=answer())
    with patch.object(voice_worker, "ask_brain", brain), \
         patch.object(voice_worker, "save_event", AsyncMock()):
        return [await voice_worker.process_one(rec, OWNER, rec.ask) for _ in range(times)]


@uses_db
async def test_high_confidence_opens_urgent_task():
    await accept_voice_command(help_cmd(), x_internal_secret=SECRET)
    rec = Recorder()
    assert await _run(rec, 2) == [True, False]
    [task] = await tasks_in(ROOM)
    assert task.task_id == "help-vc-help1"
    assert (task.priority, task.auto_pickup, task.responsible) == (0, False, "Claude")
    assert task.project == "Vera"
    assert SAFETY_NOTE in task.next_action and INSTRUCTION in task.next_action
    kinds = {r["kind"]: r["ref"] for r in task.refs}
    assert kinds["voice_command"] == "vc-help1"
    assert kinds["source"] == "s-9@5.0-9.0" and "voice_event" in kinds
    [post] = await messages_in(ROOM)
    assert (post.status, post.task_id) == ("request", "help-vc-help1")
    assert rec.sent[0] == help_worker.opened_ack_text(INSTRUCTION)
    assert rec.sent[1].startswith("Смотрю логи.")
    assert (await queue_row("vc-help1")).help_state == "opened"
    assert await tasks_in("main") == [] and await messages_in("main") == []


@uses_db
async def test_repeat_and_bot_restart_give_one_task():
    for _ in range(2):
        await accept_voice_command(help_cmd(), x_internal_secret=SECRET)
    rec = Recorder()
    crashed = AsyncMock(side_effect=RuntimeError("бот упал до «Услышала»"))
    brain = AsyncMock(return_value=answer())
    with patch.object(voice_worker, "ask_brain", brain),          patch.object(voice_worker, "save_event", AsyncMock()):
        assert await voice_worker.process_one(crashed, OWNER, rec.ask)
        row = await queue_row("vc-help1")
        assert (row.status, row.help_state) == ("pending", "opened")
        async with get_session() as s:
            await s.execute(update(VoiceCommandRow).values(next_attempt_at=None))
        assert await voice_worker.process_one(rec, OWNER, rec.ask)
    assert len(await tasks_in(ROOM)) == 1 and len(await messages_in(ROOM)) == 1
    assert rec.sent[0] == help_worker.opened_ack_text(INSTRUCTION)


@uses_db
async def test_reprompt_asks_again_without_task():
    await accept_voice_command(help_cmd("vc-r", kind="reprompt", instruction=""),
                               x_internal_secret=SECRET)
    rec = Recorder()
    await _run(rec)
    assert rec.sent == [help_worker.REPROMPT_TEXT]
    assert await tasks_in(ROOM) == []
    assert (await queue_row("vc-r")).status == "done"


@pytest.mark.parametrize("bad", [
    {"kind": "command", "instruction": "  "},
    {"fragment": [{"start": i, "end": i + 1, "text": "x"} for i in range(5)]},
    {"confidence": 1.5},
])
async def test_gateway_rejects_malformed(bad):
    with pytest.raises(ValidationError):
        VoiceCommand.model_validate({**help_cmd().model_dump(), **bad})


@pytest.mark.parametrize("why", [{"confidence": 0.7}, {"doubts": ["mid_sentence"]}])
@uses_db
async def test_borderline_waits_for_yes(why):
    await accept_voice_command(help_cmd("vc-b", **why), x_internal_secret=SECRET)
    rec = Recorder()
    await _run(rec)
    assert rec.asked == ["vc-b"]
    assert rec.sent == [help_worker.confirm_text(INSTRUCTION)]
    assert await tasks_in(ROOM) == []
    assert await claim_command() is None   # ждёт ответа, а не крутится в очереди
    assert parse_callback("vh:y:vc-b") == ("vc-b", True)
    assert await handle_confirmation("vc-b", True) == "Принято — завожу срочную задачу."
    await _run(rec)
    assert [t.task_id for t in await tasks_in(ROOM)] == ["help-vc-b"]


@uses_db
async def test_no_and_silence_never_execute():
    for cid in ("vc-no", "vc-quiet"):
        await accept_voice_command(help_cmd(cid, confidence=0.5), x_internal_secret=SECRET)
    rec = Recorder()
    await _run(rec, 2)
    assert await answer_confirmation("vc-no", False, utc_naive_now()) == "declined"
    later = utc_naive_now() + timedelta(minutes=11)
    assert await help_worker.expire_confirmations(rec, later) == 1
    assert rec.sent[-1] == help_worker.EXPIRED_TEXT
    assert await answer_confirmation("vc-quiet", True, later) == "unknown"
    assert await _run(rec) == [False]
    assert await tasks_in(ROOM) == []
    assert (await queue_row("vc-quiet")).help_state == "expired"
    assert (await queue_row("vc-no")).instruction == ""


@uses_db
async def test_late_yes_is_expired():
    await accept_voice_command(help_cmd("vc-late", confidence=0.5), x_internal_secret=SECRET)
    await _run(Recorder())
    late = utc_naive_now() + timedelta(minutes=10, seconds=1)
    assert await answer_confirmation("vc-late", True, late) == "expired"
    assert await tasks_in(ROOM) == []


@uses_db
async def test_legacy_listener_keeps_old_path():
    await accept_voice_command(help_cmd("vc-old", confidence=None), x_internal_secret=SECRET)
    rec = Recorder()
    await _run(rec)
    assert rec.sent[0] == voice_worker.ack_text(INSTRUCTION)
    assert await tasks_in(ROOM) == []


@uses_db
async def test_secrets_never_reach_the_room():
    key = "sk-" + "a1B2" * 8
    hexed = "deadbeef" * 5
    said = (f"ключ {key}, пароль qwerty123 от сервера, карта 4111 1111 1111 1111, "
            f"хэш {hexed}")
    await accept_voice_command(help_cmd("vc-s", instruction=said), x_internal_secret=SECRET)
    await _run(Recorder())
    [task] = await tasks_in(ROOM)
    [post] = await messages_in(ROOM)
    for text in (task.title, task.next_action, post.body):
        for leaked in (key, "qwerty123", "4111 1111 1111 1111", hexed):
            assert leaked not in text
        assert MASK in text or text == task.title
