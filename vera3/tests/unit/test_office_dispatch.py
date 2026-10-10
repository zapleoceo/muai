"""Диспетчер офиса: одно сообщение получателю на задачу, ref, release в open, без «Взял: vera»."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from bot_telegram import dispatcher, help_worker, voice_worker
from bot_telegram.dispatcher import dispatch_one
from gateway.voice_command import accept_voice_command
from vera_shared.room import tasks
from vera_shared.room.task_queue import next_task
from vera_shared.timeutil import utc_naive_now
from vera_shared.voice_help.hold import ask_owner_once
from vera_shared.voice_help.tracking import task_holder
from voice_help_kit import (
    OWNER,
    ROOM,
    SECRET,
    Recorder,
    answer,
    help_cmd,
    messages_in,
    tasks_in,
)

pytestmark = pytest.mark.asyncio
uses_db = pytest.mark.usefixtures("sqlite_db")


@pytest.fixture(autouse=True)
def _test_room(monkeypatch):
    monkeypatch.setenv("VOICE_HELP_ROOM", ROOM)


async def _open(task_id: str = "T1", *, refs: list[dict] | None = None, project: str = "Vera",
                title: str = "починить бота") -> None:
    await tasks.open_task(room=ROOM, task_id=task_id, agent="vera", title=title, paths=None,
                          project=project, priority=1, auto_pickup=True, refs=refs)


def _dispatch_msgs(msgs):
    return [m for m in msgs if m.message_id.startswith("dispatch-")]


@uses_db
async def test_empty_queue_makes_no_writes():
    assert await dispatch_one() is False
    assert await messages_in(ROOM) == [] and await tasks_in(ROOM) == []


@uses_db
async def test_one_task_one_post_ref_released_open():
    await _open(refs=[{"kind": "source", "ref": "result_recipient: dot"}])
    assert await dispatch_one() is True
    msgs = _dispatch_msgs(await messages_in(ROOM))
    assert len(msgs) == 1
    m = msgs[0]
    assert (m.message_id, m.to_agent, m.task_id, m.from_agent, m.status) == (
        "dispatch-T1", "dot", "T1", "vera", "request")
    assert "origin: vera" in m.body and "починить бота" in m.body
    (row,) = await tasks_in(ROOM)
    assert row.status == "open" and row.lease_holder is None
    assert any(r["ref"] == "dispatched: dispatch-T1" for r in row.refs)


@uses_db
async def test_default_recipient_is_dot_and_custom_recipient_honoured():
    await _open("T1")
    await _open("T2", refs=[{"kind": "source", "ref": "result_recipient: codex"}])
    assert await dispatch_one() and await dispatch_one()
    to = {m.task_id: m.to_agent for m in _dispatch_msgs(await messages_in(ROOM))}
    assert to == {"T1": "dot", "T2": "codex"}


@uses_db
async def test_idempotent_on_repeat_and_restart():
    await _open()
    assert await dispatch_one() is True
    assert await dispatch_one() is False  # уже с ref dispatched — не берётся
    assert len(_dispatch_msgs(await messages_in(ROOM))) == 1


@uses_db
async def test_restart_after_post_before_ref_does_not_duplicate_post():
    await _open()
    task = await next_task(room=ROOM, agent="vera", project="Vera", lease_seconds=60,
                           session=None, account=None)
    await dispatcher._post(task)  # процесс «упал» до ref и release
    await tasks.release(room=ROOM, task_id="T1", agent="vera",
                        fencing_token=task["fencing_token"], status="open")
    assert await dispatch_one() is True
    assert len(_dispatch_msgs(await messages_in(ROOM))) == 1
    (row,) = await tasks_in(ROOM)
    assert any(r["ref"].startswith("dispatched:") for r in row.refs)


@uses_db
async def test_held_task_with_open_question_is_skipped():
    await _open()
    await ask_owner_once(ROOM, "T1")
    assert await dispatch_one() is False
    assert _dispatch_msgs(await messages_in(ROOM)) == []


@uses_db
async def test_other_project_and_held_lease_are_skipped():
    await _open("T1", project="itstep")
    await _open("T2")
    held = await tasks.claim(room=ROOM, task_id="T2", agent="codex", lease_seconds=600)
    assert held["status"] == "in_progress"
    assert await dispatch_one() is False


@uses_db
async def test_fencing_respected_when_lease_lost():
    await _open()

    async def steal(task):
        await tasks.release(room=ROOM, task_id="T1", agent="vera",
                            fencing_token=task["fencing_token"], status="open")
        await tasks.claim(room=ROOM, task_id="T1", agent="codex", lease_seconds=600)
        return "dispatch-T1"

    with patch.object(dispatcher, "_post", steal):
        assert await dispatch_one() is True
    (row,) = await tasks_in(ROOM)
    assert row.lease_holder == "codex" and row.status == "in_progress"
    assert not any(r["ref"].startswith("dispatched:") for r in row.refs)


@uses_db
async def test_post_failures_count_then_block():
    await _open()
    with patch.object(dispatcher, "_post", AsyncMock(side_effect=RuntimeError("down"))):
        for attempt in (1, 2):
            assert await dispatch_one() is True
            (row,) = await tasks_in(ROOM)
            assert row.status == "open"
            assert any(r["ref"] == f"dispatch_attempts: {attempt}" for r in row.refs)
        assert await dispatch_one() is True
    (row,) = await tasks_in(ROOM)
    assert row.status == "blocked" and row.note == dispatcher.BLOCKED_NOTE
    assert await dispatch_one() is False


@uses_db
async def test_secret_in_title_is_redacted_in_post():
    await _open(title="деплой, пароль: hunter2hunter2 sk-abcdefghijklmnopqrstuvwxyz123456")
    await dispatch_one()
    (m,) = _dispatch_msgs(await messages_in(ROOM))
    assert "hunter2hunter2" not in m.body and "sk-abcdefghij" not in m.body


@uses_db
async def test_vera_claim_sends_no_taken_to_owner():
    await accept_voice_command(help_cmd(), x_internal_secret=SECRET)
    rec = Recorder()
    with patch.object(voice_worker, "ask_brain", AsyncMock(return_value=answer())), \
         patch.object(voice_worker, "save_event", AsyncMock()):
        await voice_worker.process_one(rec, OWNER)
    rec.sent.clear()
    assert await dispatch_one() is True
    assert any(m.message_id.startswith("dispatch-help-") for m in await messages_in(ROOM))

    assert await task_holder(ROOM, "help-vc-help1") == (None, "open")
    assert await help_worker.track_help(rec, utc_naive_now()) == 0
    assert not any("Взял" in s for s in rec.sent)
    # реальный исполнитель берёт задачу после диспетчера — «Взял: codex»
    await tasks.claim(room=ROOM, task_id="help-vc-help1", agent="codex", lease_seconds=600)
    later = utc_naive_now() + timedelta(seconds=1)
    await help_worker.track_help(rec, later)
    assert rec.sent == ["Взял: codex"]
