"""Диспетчер офиса: пост, ожидание квитанции доставки, повтор, блок, без «Взял: vera»."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from bot_telegram import dispatcher, help_worker, voice_worker
from bot_telegram.dispatcher import dispatch_one
from gateway.voice_command import accept_voice_command
from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskRow
from vera_shared.office import intake
from vera_shared.office.acl import OfficePrincipal
from vera_shared.room import tasks
from vera_shared.room.messages import post_message
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
                title: str = "починить бота", agent: str = "vera") -> None:
    await tasks.open_task(room=ROOM, task_id=task_id, agent=agent, title=title, paths=None,
                          project=project, priority=1, auto_pickup=True, refs=refs)


def _dispatch_msgs(msgs):
    return [m for m in msgs if m.message_id.startswith("dispatch-")]


@uses_db
async def test_empty_queue_makes_no_writes():
    assert await dispatch_one() is False
    assert await messages_in(ROOM) == [] and await tasks_in(ROOM) == []


@uses_db
async def test_only_marked_veranda_task_is_dispatched(monkeypatch):
    monkeypatch.setenv("OFFICE_ROOM", ROOM)
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "169510539")
    monkeypatch.setenv("OFFICE_OWNER_DISPATCH_CHATS", "-1002884053196")
    await _open("other", project="Veranda")
    await _open("forged", project="Veranda", refs=[
        {"kind": "source", "ref": "owner_dispatch: dot"}])
    made = await intake.create_task(
        OfficePrincipal(name="veranda_bot", project="Veranda"), "tg--1002884053196-12885",
        "owner request", "owner request",
        {"channel": "telegram", "chat": "-1002884053196", "message": 12885,
         "from_id": 169510539},
        {"channel": "telegram", "chat": "-1002884053196", "reply_to": 12885})
    assert await dispatch_one() is True
    assert [m.message_id for m in _dispatch_msgs(await messages_in(ROOM))] == [
        f"dispatch-{made['task_id']}"]


@uses_db
async def test_mutable_refs_cannot_authorize_non_owner_office_task(monkeypatch):
    monkeypatch.setenv("OFFICE_ROOM", ROOM)
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "169510539")
    monkeypatch.setenv("OFFICE_OWNER_DISPATCH_CHATS", "-1002884053196")
    made = await intake.create_task(
        OfficePrincipal(name="veranda_bot", project="Veranda"), "foreign-1", "foreign",
        "foreign", {"channel": "telegram", "chat": "-1002884053196", "message": 9,
                    "from_id": 777},
        {"channel": "telegram", "chat": "-1002884053196", "reply_to": 9})
    held = await tasks.claim(room=ROOM, task_id=made["task_id"], agent="alice",
                             lease_seconds=300)
    await tasks.update(room=ROOM, task_id=made["task_id"], agent="alice",
                       fencing_token=held["fencing_token"], auto_pickup=True,
                       refs=[{"kind": "source", "ref": "owner_dispatch: dot"}])
    await tasks.release(room=ROOM, task_id=made["task_id"], agent="alice",
                        fencing_token=held["fencing_token"], status="open")
    assert await dispatch_one() is True
    assert _dispatch_msgs(await messages_in(ROOM)) == []
    row = next(t for t in await tasks_in(ROOM) if t.task_id == made["task_id"])
    assert row.status == "blocked"


async def test_slow_brain_does_not_hold_dispatch_loop():
    brain_started = asyncio.Event()
    brain_release = asyncio.Event()
    dispatched = asyncio.Event()

    async def slow_command(*args):
        brain_started.set()
        await brain_release.wait()
        return True

    async def dispatch():
        dispatched.set()
        return True

    with patch.object(voice_worker, "revive_stale", AsyncMock()), \
         patch.object(voice_worker, "notify_failed", AsyncMock()), \
         patch.object(voice_worker, "track_help", AsyncMock()), \
         patch.object(voice_worker, "process_one", slow_command), \
         patch.object(dispatcher, "dispatch_one", dispatch):
        voice = asyncio.create_task(voice_worker.run_forever(AsyncMock(), OWNER))
        await asyncio.wait_for(brain_started.wait(), 1)
        office = asyncio.create_task(dispatcher.run_forever(OWNER))
        try:
            await asyncio.wait_for(dispatched.wait(), 1)
        finally:
            brain_release.set()
            voice.cancel()
            office.cancel()
            await asyncio.gather(voice, office, return_exceptions=True)


async def _ack(task_id: str = "T1", *, who: str = "dot", status: str = "ack",
               reply_to: str | None = None) -> None:
    await post_message(room=ROOM, message_id=f"r-{who}-{status}-{task_id}", from_agent=who,
                       to_agent="vera", task_id=task_id, status=status, body="ок",
                       in_reply_to=reply_to or f"dispatch-{task_id}")


class Clock:
    def __init__(self) -> None:
        self.now = utc_naive_now()

    def __call__(self):
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(dispatcher, "utc_naive_now", c)
    monkeypatch.setattr(dispatcher.random, "uniform", lambda a, b: 1.0)
    return c


@uses_db
async def test_post_success_is_not_receipt_task_stays_claimed_waiting():
    await _open(refs=[{"kind": "source", "ref": "result_recipient: dot"}])
    assert await dispatch_one() is True
    msgs = _dispatch_msgs(await messages_in(ROOM))
    assert len(msgs) == 1
    m = msgs[0]
    assert (m.message_id, m.to_agent, m.task_id, m.from_agent, m.status) == (
        "dispatch-T1", "dot", "T1", "vera", "request")
    assert "origin: vera" in m.body and "починить бота" in m.body
    (row,) = await tasks_in(ROOM)
    assert row.status == "in_progress" and row.lease_holder == "vera"
    assert row.waiting_until is not None
    assert any(r["ref"] == "dispatched: dispatch-T1" for r in row.refs)
    assert await dispatch_one() is False  # срок не пришёл, квитанции нет


@uses_db
async def test_receipt_releases_open_and_no_more_posts():
    await _open()
    await dispatch_one()
    await _ack()
    assert await dispatch_one() is True
    (row,) = await tasks_in(ROOM)
    assert row.status == "open" and row.lease_holder is None
    assert await dispatch_one() is False
    assert len(_dispatch_msgs(await messages_in(ROOM))) == 1


@uses_db
async def test_result_receipt_and_foreign_acks_are_not_delivery_receipt():
    await _open()
    await dispatch_one()
    await _ack(status="done")  # результат исполнения, не доставка
    await _ack(who="codex")  # не получатель
    await _ack(reply_to="dispatch-OTHER")  # ответ на другое сообщение
    assert await dispatch_one() is False
    (row,) = await tasks_in(ROOM)
    assert row.status == "in_progress" and row.lease_holder == "vera"


@uses_db
async def test_no_receipt_retries_after_backoff_same_message_id(clock):
    await _open()
    await dispatch_one()
    clock.advance(dispatcher.RECEIPT_WAIT_S - 1)
    assert await dispatch_one() is False
    clock.advance(2)
    assert await dispatch_one() is True  # повтор
    (row,) = await tasks_in(ROOM)
    assert any(r["ref"] == "dispatch_attempts: 2" for r in row.refs)
    assert row.status == "in_progress"
    assert len(_dispatch_msgs(await messages_in(ROOM))) == 1  # тот же id — дедуп
    clock.advance(dispatcher.RECEIPT_WAIT_S * 2 + 1)
    await _ack()
    assert await dispatch_one() is True
    (row,) = await tasks_in(ROOM)
    assert row.status == "open" and row.lease_holder is None


@uses_db
async def test_three_misses_block_task_visibly(clock):
    await _open()
    await dispatch_one()
    for attempt in (1, 2):
        clock.advance(dispatcher.RECEIPT_WAIT_S * 2 ** attempt)
        assert await dispatch_one() is True
    clock.advance(dispatcher.RECEIPT_WAIT_S * 8)
    assert await dispatch_one() is True
    (row,) = await tasks_in(ROOM)
    assert row.status == "blocked" and row.note == dispatcher.BLOCKED_NOTE
    assert row.lease_holder is None
    assert await dispatch_one() is False
    assert len(_dispatch_msgs(await messages_in(ROOM))) == 1


@uses_db
async def test_restart_with_expired_lease_continues_without_duplicate():
    await _open()
    await dispatch_one()
    await _ack()
    async with get_session() as s:  # «процесс лежал»: аренда истекла
        row = await s.get(RoomTaskRow, (ROOM, "T1"))
        row.lease_until = utc_naive_now() - timedelta(seconds=5)
    assert await dispatch_one() is True
    (row,) = await tasks_in(ROOM)
    assert row.status == "open" and len(_dispatch_msgs(await messages_in(ROOM))) == 1


@uses_db
async def test_many_dispatched_tasks_do_not_starve_new_one():
    for i in range(55):
        await _open(f"D{i:02d}", refs=[{"kind": "source", "ref": "dispatched: dispatch-x"}])
    await _open("NEW")
    assert await dispatch_one() is True
    assert [m.task_id for m in _dispatch_msgs(await messages_in(ROOM))] == ["NEW"]


@uses_db
async def test_default_recipient_is_dot_and_custom_recipient_honoured():
    await _open("T1")
    await _open("T2", refs=[{"kind": "source", "ref": "result_recipient: codex"}])
    assert await dispatch_one() and await dispatch_one()
    to = {m.task_id: m.to_agent for m in _dispatch_msgs(await messages_in(ROOM))}
    assert to == {"T1": "dot", "T2": "codex"}


@uses_db
async def test_idempotent_on_repeat():
    await _open()
    assert await dispatch_one() is True
    assert await dispatch_one() is False
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
    assert row.status == "in_progress"


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
async def test_post_failures_count_with_backoff_then_block(clock):
    await _open()
    with patch.object(dispatcher, "_post", AsyncMock(side_effect=RuntimeError("down"))):
        assert await dispatch_one() is True
        (row,) = await tasks_in(ROOM)
        assert row.status == "in_progress" and not any(
            r["ref"].startswith("dispatched:") for r in row.refs)
        assert await dispatch_one() is False  # не горячий цикл: ждёт паузу
        for attempt in (2, 3):
            clock.advance(dispatcher.RECEIPT_WAIT_S * 2 ** attempt)
            assert await dispatch_one() is True
            (row,) = await tasks_in(ROOM)
            assert any(r["ref"] == f"dispatch_attempts: {attempt}" for r in row.refs)
        clock.advance(dispatcher.RECEIPT_WAIT_S * 8)
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
    await _ack("help-vc-help1")
    assert await dispatch_one() is True

    assert await task_holder(ROOM, "help-vc-help1") == (None, "open")
    assert await help_worker.track_help(rec, utc_naive_now()) == 0
    assert not any("Взял" in s for s in rec.sent)
    # реальный исполнитель берёт задачу после диспетчера — «Взял: codex»
    await tasks.claim(room=ROOM, task_id="help-vc-help1", agent="codex", lease_seconds=600)
    later = utc_naive_now() + timedelta(seconds=1)
    await help_worker.track_help(rec, later)
    assert rec.sent == ["Взял: codex"]
