"""Срочная задача из голоса на живом Postgres: два исполнителя — один держатель.

На SQLite `SELECT … FOR UPDATE` пуст, и два одновременных `claim` там оба
«побеждают» (проверено 10.10.2026 на SQLite-фикстуре юнит-тестов) — поэтому
гарантия проверяется здесь. Комната `voice-test`, не `main`.
"""
from __future__ import annotations

import asyncio
import os

import pytest
import pytest_asyncio

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://vera:test@localhost:5433/vera_test")

pytestmark = [
    pytest.mark.skipif(not os.environ.get("RUN_INTEGRATION_TESTS"),
                       reason="Set RUN_INTEGRATION_TESTS=1 + provide TEST_DATABASE_URL"),
    pytest.mark.asyncio,
]


@pytest_asyncio.fixture
async def pg_db(monkeypatch, pg_schema_reset):
    monkeypatch.setenv("DATABASE_URL", TEST_DB_URL)
    monkeypatch.setenv("VOICE_HELP_ROOM", "voice-test")
    import vera_shared.db.engine as engine_mod
    from vera_shared.db.engine import close_engine, get_session, init_engine

    if engine_mod._engine is not None:
        await close_engine()
    engine = await init_engine(TEST_DB_URL)
    async with engine.begin() as conn:
        await pg_schema_reset(conn)
    yield get_session
    await close_engine()


@pytest.mark.usefixtures("pg_db")
async def test_two_executors_on_help_task_one_holder():
    from vera_shared.room.tasks import TaskBusy, claim
    from vera_shared.voice_help.room_intake import open_help_task

    task_id = await open_help_task(command_id="vc-pg", event_id=1,
                                   instruction="упал деплой", source={})
    results = await asyncio.gather(
        *(claim(room="voice-test", task_id=task_id, agent=a, lease_seconds=60)
          for a in ("claude", "codex")), return_exceptions=True)
    winners = [r for r in results if isinstance(r, dict)]
    assert len(winners) == 1 and winners[0]["fencing_token"] == 1
    assert all(isinstance(r, TaskBusy) for r in results if not isinstance(r, dict))
