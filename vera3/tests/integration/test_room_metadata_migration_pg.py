"""Migration 057 on Postgres: audit preservation, lock modes, and timeout."""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text as sa_text
from sqlalchemy.exc import DBAPIError, IntegrityError

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://vera:test@localhost:5433/vera_test")
MIGRATION = (Path(__file__).resolve().parents[2] / "infra" / "migrations"
             / "057_room_task_metadata_event.sql")
CONSTRAINT = "ck_room_task_events_kind"
OLD_KINDS = ("created", "claimed", "progress", "heartbeat", "paused", "resumed", "review",
             "blocked", "unblocked", "question", "answered", "ack_answer", "handoff_offer",
             "handoff_accept", "released", "done", "lease_expired", "watchdog_action",
             "waiting", "delegated_attestation")
NEW_KIND = "metadata_updated"

pytestmark = [
    pytest.mark.skipif(not os.environ.get("RUN_INTEGRATION_TESTS"),
                       reason="Set RUN_INTEGRATION_TESTS=1 + provide TEST_DATABASE_URL"),
    pytest.mark.asyncio,
]


def _steps() -> list[list[str]]:
    """Транзакции файла миграции (BEGIN … COMMIT) → списки операторов без BEGIN/COMMIT."""
    body = "\n".join(ln for ln in MIGRATION.read_text(encoding="utf-8").splitlines()
                     if not ln.lstrip().startswith("--"))
    steps = []
    for chunk in body.split("COMMIT;")[:-1]:
        stmts = [s.strip() for s in chunk.split(";") if s.strip()]
        steps.append([s for s in stmts if s.upper() != "BEGIN"])
    return steps


@pytest_asyncio.fixture
async def engine(pg_schema_reset):
    from sqlalchemy.ext.asyncio import create_async_engine
    eng = create_async_engine(TEST_DB_URL)
    async with eng.begin() as conn:
        await pg_schema_reset(conn)
        await conn.execute(sa_text(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version text PRIMARY KEY, note text, applied_at timestamp DEFAULT now())"))
        await _establish_previous_constraint(conn)
    yield eng
    await eng.dispose()


async def _establish_previous_constraint(conn) -> None:
    """Restore the previous CHECK containing 20 event kinds."""
    await conn.execute(sa_text(f"ALTER TABLE room_task_events DROP CONSTRAINT IF EXISTS {CONSTRAINT}"))
    kinds = ", ".join(f"'{k}'" for k in OLD_KINDS)
    await conn.execute(sa_text(
        f"ALTER TABLE room_task_events ADD CONSTRAINT {CONSTRAINT} CHECK (kind IN ({kinds}))"))


async def _kinds_in_constraint(conn) -> set[str]:
    d = (await conn.execute(sa_text(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :n "
        "AND conrelid = 'room_task_events'::regclass"), {"n": CONSTRAINT})).scalar_one()
    return set(re.findall(r"'([a-z_]+)'::", d))


async def _insert_event(conn) -> None:
    await conn.execute(sa_text(
        f"INSERT INTO room_task_events (room, task_id, kind) VALUES ('r', 't', '{NEW_KIND}')"))


async def _table_lock_modes(conn) -> set[str]:
    rows = await conn.execute(sa_text(
        "SELECT l.mode FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
        "WHERE c.relname = 'room_task_events' AND l.pid = pg_backend_pid() AND l.granted"))
    return {r[0] for r in rows}


async def _run(conn, stmts: list[str]) -> None:
    for stmt in stmts:
        await conn.exec_driver_sql(stmt)


async def test_up_and_reapply_preserve_audit_and_expanded_check(engine):
    assert len(OLD_KINDS) == 20
    async with engine.connect() as c:
        trans = await c.begin()
        with pytest.raises(IntegrityError):
            await _insert_event(c)
        await trans.rollback()
    for step in _steps():
        async with engine.begin() as c:
            await _run(c, step)
    async with engine.begin() as c:
        assert await _kinds_in_constraint(c) == {*OLD_KINDS, NEW_KIND}
        await _insert_event(c)
        row = (await c.execute(sa_text(
            "SELECT 1 FROM schema_migrations "
            "WHERE version = '057_room_task_metadata_event'"))).first()
        assert row is not None
    for step in _steps():
        async with engine.begin() as c:
            await _run(c, step)
    async with engine.begin() as c:
        assert await _kinds_in_constraint(c) == {*OLD_KINDS, NEW_KIND}
        left = (await c.execute(sa_text(
            f"SELECT count(*) FROM room_task_events WHERE kind = '{NEW_KIND}'"))).scalar_one()
        assert left == 1


async def test_lock_modes_observed(engine, capsys):
    add, validate = _steps()
    async with engine.connect() as c:
        trans = await c.begin()
        await _run(c, add)
        add_modes = await _table_lock_modes(c)
        await trans.commit()
    async with engine.connect() as c:
        trans = await c.begin()
        await _run(c, validate)
        validate_modes = await _table_lock_modes(c)
        await trans.commit()
    with capsys.disabled():
        print(f"\n::notice::057 lock modes: DROP/ADD NOT VALID={sorted(add_modes)} "
              f"VALIDATE={sorted(validate_modes)}")
    assert "AccessExclusiveLock" in add_modes
    assert "ShareUpdateExclusiveLock" in validate_modes
    assert "AccessExclusiveLock" not in validate_modes


async def test_lock_timeout_fails_and_rolls_back_fully(engine):
    add, _ = _steps()
    async with engine.connect() as holder:
        htrans = await holder.begin()
        await holder.exec_driver_sql("LOCK TABLE room_task_events IN ACCESS SHARE MODE")
        async with engine.connect() as mig:
            mtrans = await mig.begin()
            with pytest.raises(DBAPIError) as err:
                await _run(mig, add)
            assert "lock" in str(err.value).lower()
            await mtrans.rollback()
        await htrans.rollback()
    async with engine.connect() as c:
        assert await _kinds_in_constraint(c) == set(OLD_KINDS)
