"""Журнал /mcp на живом Postgres: строка пишется и читается по cid, ретенция 30 дней.

Таблица создаётся из файла миграции 052 — так проверяется сам SQL, который катится на прод.
Локально: docker run -p 5433:5432 -e POSTGRES_PASSWORD=test pgvector/pgvector:pg16
"""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://vera:test@localhost:5433/vera_test")
VERA3 = Path(__file__).resolve().parents[2]

pytestmark = [
    pytest.mark.skipif(not os.environ.get("RUN_INTEGRATION_TESTS"),
                       reason="Set RUN_INTEGRATION_TESTS=1 + provide TEST_DATABASE_URL"),
    pytest.mark.asyncio,
]


def _migration_statements() -> list[str]:
    sql = (VERA3 / "infra/migrations/052_mcp_request_log.sql").read_text(encoding="utf-8")
    sql = re.sub(r"--[^\n]*", "", sql)
    parts = [p.strip() for p in sql.split(";")]
    return [p for p in parts if p and p not in ("BEGIN", "COMMIT") and not p.startswith("SET LOCAL")]


@pytest_asyncio.fixture
async def pg_db(monkeypatch, pg_schema_reset):
    monkeypatch.setenv("DATABASE_URL", TEST_DB_URL)
    import vera_shared.db.engine as engine_mod
    from vera_shared.db.engine import close_engine, get_session, init_engine

    if engine_mod._engine is not None:
        await close_engine()
    engine = await init_engine(TEST_DB_URL)
    async with engine.begin() as conn:
        await pg_schema_reset(conn)
        await conn.execute(text("DROP TABLE IF EXISTS mcp_request_log"))
        await conn.execute(text(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version VARCHAR PRIMARY KEY, note TEXT, applied_at TIMESTAMP DEFAULT NOW())"))
        for stmt in _migration_statements():
            await conn.execute(text(stmt))
    yield get_session
    await close_engine()


async def test_row_written_and_readable_by_cid(pg_db):
    from vera_mcp import request_log_store as store

    store.schedule_insert({"cid": "cid-pg-1", "method": "POST", "actor": "claude", "ua": "ua/1",
                           "rpc": "tools/call", "tool": "room_post", "rpc_id": "7",
                           "status": 200, "ms": 12, "outcome": "ok"})
    await asyncio.gather(*list(store._pending))
    async with pg_db() as session:
        rows = (await session.execute(text(
            "SELECT cid, actor, tool, status, ms, outcome, at FROM mcp_request_log "
            "WHERE cid = 'cid-pg-1'"))).all()
    assert len(rows) == 1
    assert (rows[0].actor, rows[0].tool, rows[0].status, rows[0].outcome) == (
        "claude", "room_post", 200, "ok")
    assert rows[0].at is not None


async def test_retention_deletes_only_old_rows_of_this_table(pg_db):
    script = (VERA3 / "scripts/prune_usage_log.sql").read_text(encoding="utf-8")
    blocks = [b + "END $$;" for b in script.split("END $$;") if "DO $$" in b]
    async with pg_db() as session:
        await session.execute(text(
            "INSERT INTO mcp_request_log (cid, at) VALUES "
            "('old', now() - interval '31 days'), ('fresh', now() - interval '29 days')"))
    from vera_shared.db.engine import get_engine

    async with get_engine().connect() as conn:
        autocommit = await conn.execution_options(isolation_level="AUTOCOMMIT")
        for block in blocks:
            await autocommit.exec_driver_sql(block)
    async with pg_db() as session:
        left = (await session.execute(text("SELECT cid FROM mcp_request_log"))).scalars().all()
    assert left == ["fresh"]
