"""load_report на живом Postgres: window-запросы, `has_key` и парное событие блокера.

На SQLite `ev.data.has_key(...)` не исполняется, поэтому весь путь load_report проверяется
только здесь. В CI Postgres поднимается сервисом (deploy.yml).
"""
from __future__ import annotations

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
    import vera_shared.db.engine as engine_mod
    from vera_shared.db import models, models_room  # noqa: F401
    from vera_shared.db.engine import close_engine, init_engine

    if engine_mod._engine is not None:
        await close_engine()
    engine = await init_engine(TEST_DB_URL)
    async with engine.begin() as conn:
        await pg_schema_reset(conn)
    yield
    await close_engine()


async def held(task_id: str) -> int:
    from vera_shared.room import tasks

    task = await tasks.claim(room="main", task_id=task_id, agent="claude", lease_seconds=600)
    return task["fencing_token"]


def cards_by_id(report) -> dict:
    return {c.row.task_id: c for c in report.cards}


async def test_empty_database_loads_without_error(pg_db):
    from dashboard.office_hourly_service import load_report

    report = await load_report()
    assert report.cards == []
    assert sum(report.counts.values()) == 0


async def test_no_blocked_tasks_loads_and_counts_working(pg_db):
    from dashboard.office_hourly_service import load_report

    await held("P1")
    report = await load_report()
    assert cards_by_id(report)["P1"].status == "В работе"
    assert report.counts["В работе"] == 1


async def test_blocked_flip_with_paired_note_is_confirmed_with_reason(pg_db):
    from dashboard.office_hourly_service import load_report
    from vera_shared.room import tasks

    token = await held("P2")
    await tasks.update(room="main", task_id="P2", agent="claude", fencing_token=token,
                       status="blocked", note="reason")
    card = cards_by_id(await load_report())["P2"]
    assert card.status == "Блокер"
    assert card.info.blocker_confidence == "подтверждён"
    assert card.info.wait_reason == "reason"


async def test_blocked_without_note_then_unrelated_progress_is_unconfirmed(pg_db):
    from dashboard.office_hourly_service import load_report
    from vera_shared.room import tasks

    token = await held("P3")
    await tasks.update(room="main", task_id="P3", agent="claude", fencing_token=token,
                       status="blocked")
    await tasks.update(room="main", task_id="P3", agent="claude", fencing_token=token,
                       note="unrelated progress")
    card = cards_by_id(await load_report())["P3"]
    assert card.status == "Блокер"
    assert card.info.blocker_confidence == "не подтверждён"
    assert card.info.wait_reason is None


async def test_done_and_cancelled_are_absent_from_cards_and_counters(pg_db):
    from dashboard.office_hourly_service import load_report
    from vera_shared.db.engine import get_session
    from vera_shared.db.models_room import RoomTaskRow
    from vera_shared.room import tasks

    token = await held("P4")
    await tasks.release(room="main", task_id="P4", agent="claude", fencing_token=token,
                        status="done")
    await tasks.open_task(room="main", task_id="P5", agent="claude", title="t", paths=None)
    async with get_session() as s:
        row = await s.get(RoomTaskRow, ("main", "P5"))
        row.status = "cancelled"
        await s.commit()
    await held("P6")
    report = await load_report()
    assert set(cards_by_id(report)) == {"P6"}
    assert sum(report.counts.values()) == 1
