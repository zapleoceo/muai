"""Apply, repeat, and reverse 047 only in a disposable local test schema."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
from shadow_pg_support import run_migration_sql
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION_TESTS") != "1",
    reason="Set RUN_INTEGRATION_TESTS=1 and TEST_DATABASE_URL for isolated PostgreSQL",
)

ROOT = Path(__file__).resolve().parents[2]
UP = ROOT / "infra/migrations/047_shadow_brain.sql"
DOWN = ROOT / "infra/rollback/047_shadow_brain.sql"


@pytest.mark.asyncio
async def test_shadow_migration_up_repeat_down_preserves_legacy_event():
    raw_url = os.environ.get("TEST_DATABASE_URL")
    if not raw_url:
        pytest.fail("TEST_DATABASE_URL required")
    url = make_url(raw_url)
    if url.get_backend_name() != "postgresql" or url.host not in {
        "localhost",
        "127.0.0.1",
    }:
        pytest.fail("shadow migration test requires local PostgreSQL")
    if not url.database or "test" not in url.database.lower():
        pytest.fail("shadow migration test refuses a non-test database")
    schema = f"shadowmig_{uuid4().hex}"
    engine = create_async_engine(raw_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            await conn.execute(text(f'SET search_path TO "{schema}"'))
            await conn.execute(text("CREATE TABLE events(id bigint PRIMARY KEY)"))
            await conn.execute(
                text(
                    "CREATE TABLE schema_migrations(version text PRIMARY KEY,note text)"
                )
            )
            await conn.execute(text("INSERT INTO events VALUES (8123)"))
            await run_migration_sql(conn, UP)
            await run_migration_sql(conn, UP)
            await conn.execute(
                text(
                    "INSERT INTO brain_source_objects(provider,account_id,object_type,external_id) "
                    "VALUES ('instagram','owner-a','message','ig:1:2')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO brain_event_links(event_id,provider,account_id,object_type,external_id) "
                    "VALUES (8123,'instagram','owner-a','message','ig:1:2')"
                )
            )
            link = (
                await conn.execute(text("SELECT event_id FROM brain_event_links"))
            ).scalar_one()
            assert link == 8123
            assert (
                await conn.execute(text("SELECT to_regclass('brain_revision_receipts')"))
            ).scalar_one() is not None
            assert (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM schema_migrations WHERE version='047_shadow_brain'"
                    )
                )
            ).scalar_one() == 1
            await run_migration_sql(conn, DOWN)
            assert (
                await conn.execute(text("SELECT to_regclass('brain_source_objects')"))
            ).scalar_one() is None
            assert (
                await conn.execute(text("SELECT to_regclass('brain_revision_receipts')"))
            ).scalar_one() is None
            assert (
                await conn.execute(text("SELECT id FROM events"))
            ).scalar_one() == 8123
            await run_migration_sql(conn, UP)
            assert (
                await conn.execute(text("SELECT to_regclass('brain_source_objects')"))
            ).scalar_one() is not None
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()
