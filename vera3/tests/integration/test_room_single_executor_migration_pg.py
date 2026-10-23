"""Migration 060 on Postgres: idempotence, full rollback on lock_timeout, CHECK widening."""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text as sa_text
from sqlalchemy.exc import DBAPIError, IntegrityError

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://vera:test@localhost:5433/vera_test")
MIGRATION = (Path(__file__).resolve().parents[2] / "infra" / "migrations"
             / "060_room_single_executor.sql")
VERSION = "060_room_single_executor"
CONSTRAINT = "ck_room_task_events_kind"
OLD_KINDS = ("created", "claimed", "progress", "heartbeat", "paused", "resumed", "review",
             "blocked", "unblocked", "question", "answered", "ack_answer", "handoff_offer",
             "handoff_accept", "released", "done", "lease_expired", "watchdog_action",
             "waiting", "delegated_attestation", "metadata_updated", "cancelled")
NEW_KINDS = ("recovered", "start_approved", "wait_resolved", "rebind",
             "run_unverified", "question_closed")

pytestmark = [
    pytest.mark.skipif(not os.environ.get("RUN_INTEGRATION_TESTS"),
                       reason="Set RUN_INTEGRATION_TESTS=1 + provide TEST_DATABASE_URL"),
    pytest.mark.asyncio,
]


def _steps() -> list[list[str]]:
    """Транзакции файла миграции (BEGIN … COMMIT) → списки операторов без BEGIN/COMMIT."""
    body = "\n".join(ln for ln in MIGRATION.read_text(encoding="utf-8").splitlines()
                     if not ln.lstrip().startswith(("--", "\\")))
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
        await conn.execute(sa_text("DELETE FROM schema_migrations WHERE version = :v"), {"v": VERSION})
        await _establish_previous_state(conn)
    yield eng
    await eng.dispose()


async def _establish_previous_state(conn) -> None:
    """Состояние после 059: CHECK с 22 видами, без recovery_hold и room_singletons."""
    await conn.execute(sa_text("DROP TABLE IF EXISTS room_singletons"))
    await conn.execute(sa_text("ALTER TABLE room_tasks DROP COLUMN IF EXISTS recovery_hold"))
    await conn.execute(sa_text(f"ALTER TABLE room_task_events DROP CONSTRAINT IF EXISTS {CONSTRAINT}"))
    kinds = ", ".join(f"'{k}'" for k in OLD_KINDS)
    await conn.execute(sa_text(
        f"ALTER TABLE room_task_events ADD CONSTRAINT {CONSTRAINT} CHECK (kind IN ({kinds}))"))


async def _kinds_in_constraint(conn) -> set[str]:
    d = (await conn.execute(sa_text(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :n "
        "AND conrelid = 'room_task_events'::regclass"), {"n": CONSTRAINT})).scalar_one()
    return set(re.findall(r"'([a-z_]+)'::", d))


async def _validated(conn) -> bool:
    return (await conn.execute(sa_text(
        "SELECT convalidated FROM pg_constraint WHERE conname = :n "
        "AND conrelid = 'room_task_events'::regclass"), {"n": CONSTRAINT})).scalar_one()


async def _insert_event(conn, kind: str) -> None:
    await conn.execute(sa_text(
        "INSERT INTO room_task_events (room, task_id, kind) VALUES ('r', 't', :k)"), {"k": kind})


async def _has_column(conn, table: str, column: str) -> bool:
    return bool((await conn.execute(sa_text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = :t AND column_name = :c"), {"t": table, "c": column})).first())


async def _table_exists(conn, table: str) -> bool:
    return (await conn.execute(sa_text("SELECT to_regclass(:t) IS NOT NULL"),
                               {"t": table})).scalar_one()


async def _lock_modes(conn, table: str) -> set[str]:
    rows = await conn.execute(sa_text(
        "SELECT l.mode FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
        "WHERE c.relname = :t AND l.pid = pg_backend_pid() AND l.granted"), {"t": table})
    return {r[0] for r in rows}


async def _run(conn, stmts: list[str]) -> None:
    for stmt in stmts:
        await conn.exec_driver_sql(stmt)


async def _apply(engine) -> None:
    for step in _steps():
        async with engine.begin() as c:
            await _run(c, step)


async def test_double_apply_is_idempotent_and_recorded(engine):
    assert len(OLD_KINDS) == 22
    async with engine.begin() as c:
        await c.execute(sa_text(
            "INSERT INTO room_tasks (room, task_id, created_by, status, fencing_token, paths, priority, "
            "refs, depends_on, auto_pickup) VALUES ('r', 't', 'dot', 'open', 0, '[]', 2, "
            "'[]', '[]', false)"))
        await _insert_event(c, "created")
    await _apply(engine)
    await _apply(engine)
    async with engine.begin() as c:
        assert await _kinds_in_constraint(c) == {*OLD_KINDS, *NEW_KINDS}
        assert await _validated(c)
        rows = (await c.execute(sa_text(
            "SELECT count(*) FROM schema_migrations WHERE version = :v"), {"v": VERSION})).scalar_one()
        assert rows == 1
        hold = (await c.execute(sa_text(
            "SELECT recovery_hold FROM room_tasks WHERE room = 'r' AND task_id = 't'"))).scalar_one()
        assert hold is False
        info = {r[0]: (r[1], r[2]) for r in await c.execute(sa_text(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'room_singletons'"))}
        assert info == {"name": ("character varying", "NO"), "owner_run": ("character varying", "NO"),
                        "epoch": ("bigint", "NO"), "expires_at": ("timestamp without time zone", "NO")}
        pk = (await c.execute(sa_text(
            "SELECT a.attname FROM pg_index i JOIN pg_attribute a "
            "ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = 'room_singletons'::regclass AND i.indisprimary"))).scalars().all()
        assert pk == ["name"]
        left = (await c.execute(sa_text("SELECT count(*) FROM room_task_events"))).scalar_one()
        assert left == 1


async def test_validate_succeeds_with_existing_rows_and_all_old_kinds_stay_accepted(engine):
    async with engine.begin() as c:
        for kind in OLD_KINDS:
            await _insert_event(c, kind)
    await _apply(engine)
    async with engine.begin() as c:
        assert await _validated(c)
        total = (await c.execute(sa_text("SELECT count(*) FROM room_task_events"))).scalar_one()
        assert total == len(OLD_KINDS)
    for kind in (*OLD_KINDS, *NEW_KINDS):
        async with engine.begin() as c:
            await _insert_event(c, kind)


async def test_unknown_kind_is_rejected_after_migration(engine):
    await _apply(engine)
    async with engine.connect() as c:
        trans = await c.begin()
        with pytest.raises(IntegrityError):
            await _insert_event(c, "not_a_kind")
        await trans.rollback()


async def test_new_kinds_rejected_before_migration(engine):
    async with engine.connect() as c:
        trans = await c.begin()
        with pytest.raises(IntegrityError):
            await _insert_event(c, NEW_KINDS[0])
        await trans.rollback()


async def test_lock_modes_and_timing_observed(engine, capsys):
    async with engine.begin() as c:
        await c.execute(sa_text(
            "INSERT INTO room_task_events (room, task_id, kind) "
            "SELECT 'r', 't' || g, 'created' FROM generate_series(1, 900) g"))
    tasks_step, check_step, validate_step = _steps()
    modes: dict[str, set[str]] = {}
    ms: dict[str, float] = {}
    for name, step, table in (("tasks", tasks_step, "room_tasks"),
                              ("check", check_step, "room_task_events"),
                              ("validate", validate_step, "room_task_events")):
        async with engine.connect() as c:
            trans = await c.begin()
            t0 = time.perf_counter()
            await _run(c, step)
            ms[name] = (time.perf_counter() - t0) * 1000
            modes[name] = await _lock_modes(c, table)
            if name == "tasks":
                assert "room_task_events" not in await _locked_tables(c)
            if name == "check":
                assert "room_tasks" not in await _locked_tables(c)
            await trans.commit()
    with capsys.disabled():
        print(f"\n::notice::060 locks: room_tasks(ADD COLUMN)={sorted(modes['tasks'])} "
              f"room_task_events(DROP/ADD NOT VALID)={sorted(modes['check'])} "
              f"VALIDATE={sorted(modes['validate'])}; 900 rows: "
              + " ".join(f"{k}={v:.0f}ms" for k, v in ms.items()))
    assert "AccessExclusiveLock" in modes["tasks"]
    assert "AccessExclusiveLock" in modes["check"]
    assert "ShareUpdateExclusiveLock" in modes["validate"]
    assert "AccessExclusiveLock" not in modes["validate"]


async def _locked_tables(conn) -> set[str]:
    rows = await conn.execute(sa_text(
        "SELECT c.relname FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
        "WHERE l.pid = pg_backend_pid() AND l.granted"))
    return {r[0] for r in rows}


async def _hold_and_fail(engine, table: str, step: list[str]) -> None:
    async with engine.connect() as holder:
        htrans = await holder.begin()
        await holder.exec_driver_sql(f"LOCK TABLE {table} IN ACCESS SHARE MODE")
        async with engine.connect() as mig:
            mtrans = await mig.begin()
            with pytest.raises(DBAPIError) as err:
                await _run(mig, step)
            assert "lock" in str(err.value).lower()
            await mtrans.rollback()
        await htrans.rollback()


async def _recorded(conn) -> int:
    return (await conn.execute(sa_text(
        "SELECT count(*) FROM schema_migrations WHERE version = :v"), {"v": VERSION})).scalar_one()


async def test_tasks_step_lock_timeout_rolls_back_column_and_table(engine):
    tasks_step, _, _ = _steps()
    await _hold_and_fail(engine, "room_tasks", tasks_step)
    async with engine.connect() as c:
        assert not await _has_column(c, "room_tasks", "recovery_hold")
        assert not await _table_exists(c, "room_singletons")
        assert await _kinds_in_constraint(c) == set(OLD_KINDS)
        assert await _recorded(c) == 0


async def test_check_step_failure_leaves_no_applied_state(engine):
    """Симуляция psql -f с ON_ERROR_STOP: упала транзакция 2, транзакция 3 не запускается."""
    tasks_step, check_step, _ = _steps()
    async with engine.begin() as c:
        await _run(c, tasks_step)
    await _hold_and_fail(engine, "room_task_events", check_step)
    async with engine.connect() as c:
        assert await _kinds_in_constraint(c) == set(OLD_KINDS)
        assert await _validated(c)
        assert await _recorded(c) == 0


def test_migration_stops_on_error_and_splitter_skips_meta_commands():
    text = MIGRATION.read_text(encoding="utf-8")
    assert "\\set ON_ERROR_STOP on" in text
    assert len(_steps()) == 3
    assert all(not s.startswith("\\") for step in _steps() for s in step)
