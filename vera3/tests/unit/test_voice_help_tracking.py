"""Срочная задача после заведения: «Взял», эскалация dot, напоминание, один держатель.

Часы подменены: `track_help(send, now)` получает время аргументом.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from bot_telegram import help_worker, voice_worker
from gateway.voice_command import accept_voice_command
from vera_shared.room import tasks
from vera_shared.room.tasks import TaskBusy
from vera_shared.voice_help.policy import project_for
from vera_shared.voice_help.tracking import CLOSED, ESCALATE, REMIND, TAKEN, next_step
from voice_help_kit import (
    OWNER,
    ROOM,
    SECRET,
    Recorder,
    answer,
    hand_to,
    help_cmd,
    messages_in,
    queue_row,
    tasks_in,
)


def uses_db(test):
    """Асинхронный тест на SQLite-фикстуре. Метка asyncio — только на async-тестах."""
    return pytest.mark.asyncio(pytest.mark.usefixtures("sqlite_db")(test))
TASK = "help-vc-help1"


@pytest.fixture(autouse=True)
def _test_room(monkeypatch):
    monkeypatch.setenv("VOICE_HELP_ROOM", ROOM)


async def _opened() -> tuple[Recorder, object]:
    await accept_voice_command(help_cmd(), x_internal_secret=SECRET)
    rec = Recorder()
    with patch.object(voice_worker, "ask_brain", AsyncMock(return_value=answer())), \
         patch.object(voice_worker, "save_event", AsyncMock()):
        await voice_worker.process_one(rec, OWNER)
    rec.sent.clear()
    return rec, (await queue_row("vc-help1")).task_opened_at


@uses_db
async def test_escalation_and_reminder_by_the_clock():
    rec, opened = await _opened()
    assert await help_worker.track_help(rec, opened + timedelta(minutes=4)) == 0
    assert await help_worker.track_help(rec, opened + timedelta(minutes=5)) == 1
    assert rec.sent == [help_worker.ESCALATED_TEXT]
    esc = [m for m in await messages_in(ROOM) if m.to_agent == "dot"]
    assert len(esc) == 1 and (esc[0].status, esc[0].task_id) == ("request", TASK)
    assert await help_worker.track_help(rec, opened + timedelta(minutes=6)) == 0
    assert await help_worker.track_help(rec, opened + timedelta(minutes=20)) == 1
    assert rec.sent[-1] == help_worker.REMIND_TEXT
    assert await help_worker.track_help(rec, opened + timedelta(minutes=40)) == 0
    row = await queue_row("vc-help1")
    assert row.escalated_at and row.reminded_at and row.help_state == "reminded"
    await hand_to(ROOM, TASK, "codex")
    assert await help_worker.track_help(rec, opened + timedelta(minutes=41)) == 1
    assert rec.sent[-1] == help_worker.taken_text("codex")


@uses_db
async def test_taken_is_reported_once_and_no_escalation():
    rec, opened = await _opened()
    await hand_to(ROOM, TASK, "claude")
    assert await help_worker.track_help(rec, opened + timedelta(minutes=1)) == 1
    assert await help_worker.track_help(rec, opened + timedelta(minutes=30)) == 0
    assert rec.sent == [help_worker.taken_text("claude")]
    assert [m for m in await messages_in(ROOM) if m.to_agent == "dot"] == []
    assert (await queue_row("vc-help1")).taken_by == "claude"


async def _attest(task_id: str) -> None:
    from vera_shared.db.engine import get_session
    from vera_shared.room.task_events import record_event

    async with get_session() as s:
        await record_event(s, room=ROOM, task_id=task_id, kind="delegated_attestation",
                           agent="dot")


@uses_db
async def test_attested_live_claim_by_dot_is_taken_once_without_escalation():
    rec, opened = await _opened()
    await _attest(TASK)
    await tasks.claim(room=ROOM, task_id=TASK, agent="dot", lease_seconds=900)
    assert await help_worker.track_help(rec, opened + timedelta(minutes=6)) == 1
    assert await help_worker.track_help(rec, opened + timedelta(minutes=30)) == 0
    assert rec.sent == [help_worker.taken_text("dot")]
    assert [m for m in await messages_in(ROOM) if m.to_agent == "dot"] == []


@uses_db
async def test_attested_expired_claim_is_not_taken():
    from vera_shared.db.engine import get_session
    from vera_shared.db.models_room import RoomTaskRow
    from vera_shared.timeutil import utc_naive_now
    from vera_shared.voice_help.tracking import task_holder

    await _opened()
    await _attest(TASK)
    await tasks.claim(room=ROOM, task_id=TASK, agent="dot", lease_seconds=900)
    async with get_session() as s:
        row = await s.get(RoomTaskRow, (ROOM, TASK))
        row.lease_until = utc_naive_now() - timedelta(seconds=5)
    assert (await task_holder(ROOM, TASK))[0] is None


@uses_db
async def test_non_attested_claim_without_accept_is_not_taken():
    from vera_shared.voice_help.tracking import task_holder

    await _opened()
    await tasks.claim(room=ROOM, task_id=TASK, agent="dot", lease_seconds=900)
    assert (await task_holder(ROOM, TASK))[0] is None


@uses_db
async def test_second_executor_is_refused():
    """Одновременный захват — в tests/integration/test_voice_help_pg.py: на SQLite
    `FOR UPDATE` пуст, и гонку там не воспроизвести. Здесь — последовательный:
    задачу держит один, второй получает TaskBusy, держатель и токен прежние."""
    await _opened()
    first = await tasks.claim(room=ROOM, task_id=TASK, agent="claude", lease_seconds=900)
    with pytest.raises(TaskBusy):
        await tasks.claim(room=ROOM, task_id=TASK, agent="codex", lease_seconds=900)
    [task] = await tasks_in(ROOM)
    assert (task.lease_holder, task.fencing_token) == ("claude", first["fencing_token"])


def test_next_step_rules():
    m = timedelta(minutes=1)
    assert next_step("opened", 1 * m, "x", "in_progress") == TAKEN
    assert next_step("opened", 4 * m, None, "open") is None
    assert next_step("opened", 5 * m, None, "open") == ESCALATE
    assert next_step("escalated", 19 * m, None, "open") is None
    assert next_step("escalated", 20 * m, None, "open") == REMIND
    assert next_step("reminded", 60 * m, None, "open") is None
    assert next_step("opened", 1 * m, None, "cancelled") == CLOSED
    assert next_step("opened", 1 * m, None, None) == CLOSED


@pytest.mark.parametrize(("app", "window", "project"), [
    ("Code.exe", "myAI — vera3", "Vera"),
    (None, None, "Vera"),
    ("chrome.exe", "SIN-4792 — Jira", "itstep"),
    ("Telegram.exe", "Веранда — финансы", "veranda"),
])
def test_project_routing(app, window, project):
    assert project_for(app, window) == project


@uses_db
async def test_one_failing_row_does_not_stop_the_pass():
    rec, opened = await _opened()
    await accept_voice_command(help_cmd("vc-help2"), x_internal_secret=SECRET)
    with patch.object(voice_worker, "ask_brain", AsyncMock(return_value=answer())),          patch.object(voice_worker, "save_event", AsyncMock()):
        await voice_worker.process_one(rec, OWNER)
    real = help_worker.escalate
    calls = []

    async def flaky(*, task_id: str) -> None:
        calls.append(task_id)
        if len(calls) == 1:
            raise RuntimeError("комната недоступна")
        await real(task_id=task_id)
    with patch.object(help_worker, "escalate", flaky):
        assert await help_worker.track_help(rec, opened + timedelta(minutes=30)) == 1
    assert len(calls) == 2
