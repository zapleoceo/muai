"""Комната агентов на живом Postgres: порядок фиксации, время после ожидания лока,
одновременный захват одной задачи.

На SQLite эти гонки не воспроизводятся: там один писатель на базу, нет
advisory-локов и `SELECT … FOR UPDATE`. В CI Postgres поднимается сервисом
(deploy.yml); локально: docker run -p 5433:5432 -e POSTGRES_PASSWORD=test pgvector/pgvector:pg16
"""
from __future__ import annotations

import asyncio
import os
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text
from vera_shared.timeutil import utc_naive_now

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://vera:test@localhost:5433/vera_test")

pytestmark = [
    pytest.mark.skipif(not os.environ.get("RUN_INTEGRATION_TESTS"),
                       reason="Set RUN_INTEGRATION_TESTS=1 + provide TEST_DATABASE_URL"),
    pytest.mark.asyncio,
]

BLOCKED_S = 0.5


@pytest_asyncio.fixture
async def pg_db(monkeypatch, pg_schema_reset):
    monkeypatch.setenv("DATABASE_URL", TEST_DB_URL)
    import vera_shared.db.engine as engine_mod
    from vera_shared.db import models, models_room  # noqa: F401
    from vera_shared.db.engine import close_engine, get_session, init_engine

    if engine_mod._engine is not None:
        await close_engine()
    engine = await init_engine(TEST_DB_URL)
    async with engine.begin() as conn:
        await pg_schema_reset(conn)
    yield get_session
    await close_engine()


async def test_post_waits_for_an_open_writer_so_ids_follow_commit_order(pg_db):
    from vera_shared.room.messages import _lock_room, inbox, post_message

    get_session = pg_db
    async with get_session() as s:
        await _lock_room(s, "main")  # чужая запись в комнату держит лок до коммита
        late = asyncio.create_task(post_message(
            room="main", message_id="b", from_agent="claude", body="second"))
        await asyncio.sleep(BLOCKED_S)
        assert not late.done(), "запись должна ждать открытую транзакцию комнаты"
        await s.execute(text(
            "INSERT INTO room_messages (room, message_id, from_agent, status, body) "
            "VALUES ('main', 'a', 'claude', 'info', 'first')"))
    msg, _ = await late
    items, _ = await inbox(agent="codex", room="main", consumer="default", limit=10)
    assert [m["body"] for m in items] == ["first", "second"]
    assert items[-1]["id"] == msg["id"]


async def test_other_rooms_are_not_serialized(pg_db):
    from vera_shared.room.messages import _lock_room, post_message

    async with pg_db() as s:
        await _lock_room(s, "main")
        msg, _ = await asyncio.wait_for(post_message(
            room="other", message_id="x", from_agent="claude", body="free"), timeout=5)
    assert msg["room"] == "other"


async def test_claim_reads_the_clock_after_waiting_for_the_row_lock(pg_db):
    from vera_shared.db.models_room import RoomTaskRow
    from vera_shared.room.tasks import claim

    get_session = pg_db
    await claim(room="main", task_id="T", agent="claude", lease_seconds=60)
    async with get_session() as s:
        row = await s.get(RoomTaskRow, ("main", "T"), with_for_update=True)
        row.lease_until = utc_naive_now() + timedelta(seconds=BLOCKED_S / 2)
        # аренда claude истекает, пока codex ждёт эту блокировку
        taker = asyncio.create_task(claim(room="main", task_id="T", agent="codex",
                                          lease_seconds=60))
        await asyncio.sleep(BLOCKED_S)
        assert not taker.done()
    task = await taker
    assert (task["lease_holder"], task["fencing_token"]) == ("codex", 2)


async def test_concurrent_claims_of_a_new_task_have_one_winner(pg_db):
    from vera_shared.room.tasks import TaskBusy, claim

    results = await asyncio.gather(
        *(claim(room="main", task_id="NEW", agent=a, lease_seconds=60)
          for a in ("claude", "codex")), return_exceptions=True)
    winners = [r for r in results if isinstance(r, dict)]
    assert len(winners) == 1 and winners[0]["fencing_token"] == 1
    assert all(isinstance(r, TaskBusy) for r in results if not isinstance(r, dict))


async def test_tracker_columns_and_tables_exist(pg_db):
    async with pg_db() as s:
        cols = {r[0] for r in (await s.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='room_tasks'"))).all()}
        tables = {r[0] for r in (await s.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public'"))).all()}
    assert {"priority", "owner", "next_action", "refs", "holder_session", "holder_account",
            "last_progress_at", "last_progress_text", "next_checkpoint_at",
            "pending_handoff_to", "plan_start", "plan_end"} <= cols
    assert {"room_task_events", "room_task_questions", "room_task_answers",
            "watchdog_state"} <= tables


async def test_events_are_written_in_the_same_transaction(pg_db):
    from vera_shared.room.task_events import list_events
    from vera_shared.room.tasks import StaleLease, claim, update

    get_session = pg_db
    t = await claim(room="main", task_id="EV", agent="claude", lease_seconds=60,
                    session="s1", account="a1")
    await update(room="main", task_id="EV", agent="claude",
                 fencing_token=t["fencing_token"], extend_seconds=120)
    await update(room="main", task_id="EV", agent="claude",
                 fencing_token=t["fencing_token"], note="step")
    with pytest.raises(StaleLease):
        await update(room="main", task_id="EV", agent="claude", fencing_token=99, note="x")
    async with get_session() as s:
        events = await list_events(s, room="main", task_id="EV")
    assert [e["kind"] for e in events] == ["created", "claimed", "heartbeat", "progress"]
    assert events[1]["session"] == "s1" and events[1]["fencing_token"] == 1


async def test_event_kind_check_constraint_rejects_unknown(pg_db):
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        async with pg_db() as s:
            await s.execute(text(
                "INSERT INTO room_task_events (room, task_id, kind) VALUES ('m','t','bogus')"))
