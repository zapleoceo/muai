"""Решение «кто пишет финал» на живом Postgres: приём передачи против ответа мозга.

SQLite игнорирует FOR UPDATE, поэтому гонку проверяем здесь: `claim_unless_accepted`
блокирует строку задачи (её же блокирует `handoff.accept`), так что исход один из двух
и никогда «оба». Комната `voice-test`.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime

import pytest
import pytest_asyncio

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://vera:test@localhost:5433/vera_test")
ROOM = "voice-test"
TASK = "help-vc-race1"

pytestmark = [
    pytest.mark.skipif(
        not (os.environ.get("RUN_INTEGRATION_TESTS") or os.environ.get("TEST_DATABASE_URL")),
        reason="Set TEST_DATABASE_URL (or RUN_INTEGRATION_TESTS=1)"),
    pytest.mark.asyncio,
]


@pytest_asyncio.fixture
async def pg_db(monkeypatch, pg_schema_reset):
    monkeypatch.setenv("DATABASE_URL", TEST_DB_URL)
    monkeypatch.setenv("VOICE_HELP_ROOM", ROOM)
    import vera_shared.db.engine as engine_mod
    from vera_shared.db.engine import close_engine, init_engine

    if engine_mod._engine is not None:
        await close_engine()
    engine = await init_engine(TEST_DB_URL)
    async with engine.begin() as conn:
        await pg_schema_reset(conn)
    yield
    await close_engine()


async def _offered_task() -> None:
    from vera_shared.db.engine import get_session
    from vera_shared.db.models_voice import VoiceCommandRow
    from vera_shared.room import handoff, tasks

    async with get_session() as s:
        s.add(VoiceCommandRow(command_id="vc-race1", spoken_at=datetime(2026, 1, 1),
                              source={"guard": "own", "guard_version": 2}, confidence=0.95,
                              instruction="test task", help_state="opened", task_id=TASK))
    await tasks.open_task(room=ROOM, task_id=TASK, agent="vera", title="гонка",
                          paths=None)
    held = await tasks.claim(room=ROOM, task_id=TASK, agent="vera", lease_seconds=900)
    await handoff.offer(room=ROOM, task_id=TASK, agent="vera",
                        fencing_token=held["fencing_token"], to_agent="codex")


async def test_concurrent_accept_and_brain_claim_never_give_two_finals(pg_db):
    from vera_shared.db.engine import get_session
    from vera_shared.db.models_voice import VoiceCommandRow
    from vera_shared.room import handoff
    from vera_shared.voice_help.delivery_claim import claim_unless_accepted

    await _offered_task()
    verdict, _ = await asyncio.gather(
        claim_unless_accepted("vc-race1", ROOM, TASK, "brain", datetime(2026, 1, 1)),
        handoff.accept(room=ROOM, task_id=TASK, agent="codex"))
    async with get_session() as s:
        row = await s.get(VoiceCommandRow, "vc-race1")
        claimed = "delivery" in (row.source or {})
    # либо мозг взял финал до приёма, либо приём первым и заявки нет — но не оба сразу
    assert not claimed
    assert verdict[0] == "executor"


async def test_accept_done_then_brain_loses_and_executor_sends_once(pg_db, monkeypatch):
    from vera_shared.db.engine import get_session
    from vera_shared.db.models_voice import VoiceCommandRow
    from vera_shared.room import handoff, tasks
    from vera_shared.voice_help.delivery_claim import claim_unless_accepted

    monkeypatch.setenv("VOICE_RESULT_DELIVERY_SINCE", "2000-01-01T00:00:00Z")
    await _offered_task()
    await handoff.accept(room=ROOM, task_id=TASK, agent="codex")
    held = await tasks.claim(room=ROOM, task_id=TASK, agent="codex", lease_seconds=900)
    await tasks.release(room=ROOM, task_id=TASK, agent="codex",
                        fencing_token=held["fencing_token"], status="done", note="готово")
    verdict = await claim_unless_accepted(
        "vc-race1", ROOM, TASK, "brain", datetime(2026, 1, 1))
    assert verdict == ("executor", "codex")

    from bot_telegram import help_worker

    sent: list[str] = []

    async def send(_html: str, plain: str) -> int:
        sent.append(plain)
        return 42

    async with get_session() as s:
        row = await s.get(VoiceCommandRow, "vc-race1")
        s.expunge(row)
    for _ in range(2):
        async with get_session() as s:
            row = await s.get(VoiceCommandRow, "vc-race1")
            s.expunge(row)
        await help_worker.deliver_result(send, row, datetime(2026, 1, 2))
    assert sent == [f"Результат по задаче {TASK} (codex): готово"]


async def _done_row():
    from vera_shared.db.engine import get_session
    from vera_shared.db.models_voice import VoiceCommandRow
    from vera_shared.room import tasks
    held = await tasks.claim(room=ROOM, task_id=TASK, agent="vera", lease_seconds=900)
    await tasks.release(room=ROOM, task_id=TASK, agent="vera",
                        fencing_token=held["fencing_token"], status="done", note="test final")
    async with get_session() as session:
        row = await session.get(VoiceCommandRow, "vc-race1")
        session.expunge(row)
        return row


async def test_concurrent_executor_sends_and_tracker_keep_one_real_receipt(pg_db, monkeypatch):
    from bot_telegram import help_worker
    from vera_shared.db.engine import get_session
    from vera_shared.db.models_voice import VoiceCommandRow
    from vera_shared.timeutil import utc_naive_now
    monkeypatch.setenv("VOICE_RESULT_DELIVERY_SINCE", "2000-01-01T00:00:00Z")
    await _offered_task()
    row = await _done_row()
    sending, proceed = asyncio.Event(), asyncio.Event()
    calls = []

    async def send(_h, plain):
        calls.append(plain)
        sending.set()
        await proceed.wait()
        return 123

    now = utc_naive_now()
    first = asyncio.create_task(help_worker.deliver_result(send, row, now))
    await asyncio.wait_for(sending.wait(), 5)
    # A parallel sender loses the atomic reservation; a poller must not call it unknown.
    assert not await help_worker.deliver_result(send, row, now)
    await help_worker.track_help(send, now)
    async with get_session() as session:
        assert (await session.get(VoiceCommandRow, "vc-race1")).help_state == "result_pending"
    proceed.set()
    assert await first
    async with get_session() as session:
        current = await session.get(VoiceCommandRow, "vc-race1")
        assert current.status == "done" and current.help_state == "closed"
        assert current.source["delivery"]["state"] == "sent"
        assert current.source["result_message_id"] == 123 and current.answered_at
    assert len(calls) == 1


async def test_late_voice_worker_hold_cannot_reopen_delivered_row(pg_db, monkeypatch):
    from unittest.mock import patch

    from bot_telegram import help_worker, voice_worker
    from vera_shared.db.engine import get_session
    from vera_shared.db.models_voice import VoiceCommandRow
    from vera_shared.timeutil import utc_naive_now
    monkeypatch.setenv("VOICE_RESULT_DELIVERY_SINCE", "2000-01-01T00:00:00Z")
    await _offered_task()
    opened, proceed = asyncio.Event(), asyncio.Event()

    async def pause(*_args):
        opened.set()
        await proceed.wait()
        return False

    async def send(_h, _p):
        return 124

    with patch.object(voice_worker, "open_and_notify", pause):
        pending = asyncio.create_task(voice_worker.process_one(send, 1))
        await asyncio.wait_for(opened.wait(), 5)
        row = await _done_row()
        assert await help_worker.deliver_result(send, row, utc_naive_now())
        proceed.set()
        assert await pending
    async with get_session() as session:
        row = await session.get(VoiceCommandRow, "vc-race1")
        assert row.status == "done" and row.help_state == "closed" and row.instruction == ""
