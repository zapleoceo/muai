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


async def test_progress_vs_heartbeat_on_postgres(pg_db):
    from vera_shared.room import task_progress, tasks

    await tasks.claim(room="main", task_id="PG2", agent="claude", lease_seconds=600)
    beat = await tasks.update(room="main", task_id="PG2", agent="claude", fencing_token=1,
                              extend_seconds=600)
    assert beat["last_progress_at"] is None
    done = await task_progress.progress(room="main", task_id="PG2", agent="claude",
                                        fencing_token=1, result="x",
                                        next_checkpoint_seconds=120)
    assert done["last_progress_at"] and done["next_checkpoint_at"]
    events = await task_progress.history(room="main", task_id="PG2", since_id=None, limit=10)
    assert [e["kind"] for e in events] == ["created", "claimed", "heartbeat", "progress"]


async def test_concurrent_claims_of_an_existing_open_task_have_one_winner(pg_db):
    from vera_shared.room.tasks import TaskBusy, claim, open_task

    await open_task(room="main", task_id="OPEN", agent="owner", title=None, paths=None)
    results = await asyncio.gather(
        *(claim(room="main", task_id="OPEN", agent=a, lease_seconds=60, session=f"s-{a}")
          for a in ("claude", "codex")), return_exceptions=True)
    winners = [r for r in results if isinstance(r, dict)]
    assert len(winners) == 1 and winners[0]["fencing_token"] == 1
    assert all(isinstance(r, TaskBusy) for r in results if not isinstance(r, dict))


async def test_attention_constraints_and_waiting_kind_exist(pg_db):
    from sqlalchemy.exc import IntegrityError

    async with pg_db() as s:
        await s.execute(text(
            "INSERT INTO room_task_events (room, task_id, kind) VALUES ('m','t','waiting')"))
    for column, value in (("priority", "4"), ("next_action", "repeat('x', 2001)")):
        with pytest.raises(IntegrityError):
            async with pg_db() as s:
                await s.execute(text(
                    f"INSERT INTO room_tasks (room, task_id, created_by, {column}) "
                    f"VALUES ('m','bad','c', {value})"))


async def _queue_open_task(task_id: str) -> None:
    from vera_shared.room import tasks

    t = await tasks.claim(room="main", task_id=task_id, agent="owner", lease_seconds=60)
    await tasks.update(room="main", task_id=task_id, agent="owner",
                       fencing_token=t["fencing_token"], auto_pickup=True)
    await tasks.release(room="main", task_id=task_id, agent="owner",
                        fencing_token=t["fencing_token"], status="open")


def _pick(agent: str):
    from vera_shared.room.task_queue import next_task

    return next_task(room="main", agent=agent, project=None, lease_seconds=60,
                     session=None, account=None)


async def test_next_task_concurrent_pickers_get_different_tasks(pg_db):
    for tid in ("Q1", "Q2"):
        await _queue_open_task(tid)
    got = await asyncio.gather(_pick("claude"), _pick("codex"), _pick("claude"))
    assert sorted(t["task_id"] for t in got if t) == ["Q1", "Q2"]
    assert sum(t is None for t in got) == 1


async def test_next_task_skips_a_row_locked_by_another_transaction(pg_db):
    from vera_shared.db.models_room import RoomTaskRow

    for tid in ("L1", "L2"):
        await _queue_open_task(tid)
    async with pg_db() as s:
        await s.get(RoomTaskRow, ("main", "L1"), with_for_update=True)
        got = await asyncio.wait_for(_pick("claude"), timeout=5)
    assert got and got["task_id"] == "L2"


async def test_question_round_trip_on_postgres(pg_db):
    from vera_shared.room import messages, questions, task_progress, tasks

    await tasks.claim(room="main", task_id="PGQ", agent="claude", lease_seconds=600)
    q = await questions.ask(room="main", task_id="PGQ", agent="claude", fencing_token=1,
                            question="Какую БД?")
    blocked = await tasks.update(room="main", task_id="PGQ", agent="claude",
                                 fencing_token=1, extend_seconds=600)
    assert (blocked["status"], blocked["owner"]) == ("blocked", "owner")
    _, changed = await questions.answer(room="main", task_id="PGQ", qid=q["qid"], text="PG")
    _, again = await questions.answer(room="main", task_id="PGQ", qid=q["qid"], text="PG")
    assert changed and not again
    acked = await questions.ack(room="main", task_id="PGQ", qid=q["qid"], agent="claude",
                                fencing_token=1)
    assert acked["status"] == "acked" and len(acked["answers"]) == 1
    events = await task_progress.history(room="main", task_id="PGQ", since_id=None, limit=20)
    kinds = [e["kind"] for e in events]
    assert kinds[kinds.index("question"):] == [
        "question", "heartbeat", "answered", "ack_answer", "unblocked"]
    msgs = await messages.history(room="main", limit=10, task_id="PGQ")
    assert [m["status"] for m in msgs] == ["question", "info"]
    assert msgs[1]["in_reply_to"] == msgs[0]["message_id"]


async def test_handoff_and_watchdog_round_trip_on_postgres(pg_db):
    from vera_shared.db.models_room import WatchdogStateRow
    from vera_shared.room import handoff, task_progress, tasks, watchdog
    from vera_shared.room.tasks import StaleLease

    await tasks.claim(room="main", task_id="PGH", agent="claude", lease_seconds=600)
    await handoff.offer(room="main", task_id="PGH", agent="claude", fencing_token=1,
                        to_agent="codex", note="дальше ты")
    moved = await handoff.accept(room="main", task_id="PGH", agent="codex", session="s1")
    assert (moved["lease_holder"], moved["fencing_token"]) == ("codex", 2)
    with pytest.raises(StaleLease):
        await tasks.update(room="main", task_id="PGH", agent="claude", fencing_token=1,
                           note="поздно")
    late = utc_naive_now() + timedelta(hours=3)
    first = await watchdog.run_once(late)
    assert first["actions"] == [("main", "PGH", "lease_expired"),
                                ("main", "PGH", "watchdog_action")]
    assert (await watchdog.run_once(late + timedelta(minutes=1)))["actions"] == []
    kinds = [e["kind"] for e in await task_progress.history(
        room="main", task_id="PGH", since_id=None, limit=50)]
    assert kinds.count("lease_expired") == 1 and kinds[-1] == "watchdog_action"
    async with pg_db() as s:
        st = await s.get(WatchdogStateRow, "room")
    assert st.last_run_at == late + timedelta(minutes=1) and st.last_error is None
    reopened = await tasks.claim(room="main", task_id="PGH", agent="gemini",
                                 lease_seconds=600)
    assert reopened["fencing_token"] == 3
