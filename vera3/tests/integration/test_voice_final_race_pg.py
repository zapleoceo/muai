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
                              help_state="opened", task_id=TASK))
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
    assert (verdict[0] == "won") == claimed
    assert verdict[0] in ("won", "executor")
