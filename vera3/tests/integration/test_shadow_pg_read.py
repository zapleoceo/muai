"""Call the real PostgreSQL shadow read function against a disposable test DB."""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION_TESTS") != "1",
    reason="Set RUN_INTEGRATION_TESTS=1 and TEST_DATABASE_URL for isolated PostgreSQL",
)


@pytest.mark.asyncio
async def test_pg_as_of_respects_tombstone_and_current_acl():
    raw_url = os.environ.get("TEST_DATABASE_URL")
    if not raw_url:
        pytest.fail("RUN_INTEGRATION_TESTS=1 requires TEST_DATABASE_URL")
    url = make_url(raw_url)
    if url.get_backend_name() != "postgresql" or url.host not in {
        "localhost",
        "127.0.0.1",
    }:
        pytest.fail("shadow integration requires local isolated PostgreSQL")
    if not url.database or "test" not in url.database.lower():
        pytest.fail("shadow integration refuses a non-test database")
    from vera_shared.db.shadow_pg_read import fetch_claims_as_of_pg

    engine = create_async_engine(raw_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE TEMP TABLE brain_source_objects (provider text, account_id text, "
                    "object_type text, external_id text) ON COMMIT PRESERVE ROWS"
                )
            )
            await conn.execute(
                text(
                    "CREATE TEMP TABLE brain_revisions (provider text, account_id text, "
                    "object_type text, external_id text, revision text, deleted boolean) "
                    "ON COMMIT PRESERVE ROWS"
                )
            )
            await conn.execute(
                text(
                    "CREATE TEMP TABLE brain_generations (id integer, provider text, "
                    "account_id text, object_type text, external_id text, revision text, "
                    "known_from timestamptz, known_to timestamptz) ON COMMIT PRESERVE ROWS"
                )
            )
            await conn.execute(
                text(
                    "CREATE TEMP TABLE brain_claims (generation_id integer, ordinal integer, "
                    "subject text, predicate text, value text, valid_from timestamptz, "
                    "valid_to timestamptz, evidence_anchor text, evidence_kind text) "
                    "ON COMMIT PRESERVE ROWS"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO brain_source_objects VALUES ('telegram','owner-a','message','42')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO brain_revisions VALUES "
                    "('telegram','owner-a','message','42','1',false),"
                    "('telegram','owner-a','message','42','2',true)"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO brain_generations VALUES "
                    "(1,'telegram','owner-a','message','42','1',:first,:second),"
                    "(2,'telegram','owner-a','message','42','2',:second,NULL)"
                ),
                {
                    "first": datetime(2026, 10, 3, 1, tzinfo=UTC),
                    "second": datetime(2026, 10, 3, 2, tzinfo=UTC),
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO brain_claims VALUES "
                    "(1,0,'project','decision','ship',:valid,NULL,'message:0-4','document')"
                ),
                {"valid": datetime(2026, 10, 2, tzinfo=UTC)},
            )
            registry = {("telegram", "owner-a"): "telegram:owner-a"}
            before = await fetch_claims_as_of_pg(
                conn, registry, {"telegram:owner-a"}, "2026-10-03T01:30:00Z"
            )
            assert [row["value"] for row in before] == ["ship"]
            assert (
                await fetch_claims_as_of_pg(
                    conn, registry, {"telegram:owner-a"}, "2026-10-03T02:30:00Z"
                )
                == []
            )
            assert (
                await fetch_claims_as_of_pg(
                    conn, registry, set(), "2026-10-03T01:30:00Z"
                )
                == []
            )
    finally:
        await engine.dispose()
