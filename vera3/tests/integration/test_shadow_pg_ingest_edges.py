"""Cross-account, tombstone, and concurrent object cases on real PostgreSQL."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace

import pytest
from shadow_pg_support import (
    isolated_shadow_schema,
    sample_claim,
    sample_source,
    use_schema,
)
from sqlalchemy import text

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION_TESTS") != "1",
    reason="Set RUN_INTEGRATION_TESTS=1 and TEST_DATABASE_URL for isolated PostgreSQL",
)


@pytest.mark.asyncio
async def test_pg_tombstone_and_cross_account_identity():
    from vera_shared.db.shadow_pg_ingest import PgShadowIngest
    from vera_shared.db.shadow_pg_read import fetch_claims_as_of_pg

    registry = {
        ("telegram", "owner-a"): "telegram:owner-a",
        ("telegram", "owner-b"): "telegram:owner-b",
    }
    ingest = PgShadowIngest(registry)
    async with (
        isolated_shadow_schema() as (engine, schema),
        engine.connect() as conn,
    ):
        await use_schema(conn, schema)
        await ingest.apply(
            conn, sample_source(), [sample_claim("a")], known_at="2026-10-03T01:00:00Z"
        )
        await ingest.apply(
            conn,
            sample_source(account="owner-b"),
            [sample_claim("b")],
            known_at="2026-10-03T01:00:00Z",
        )
        await ingest.apply(
            conn,
            replace(sample_source("2"), deleted=True),
            [],
            known_at="2026-10-03T02:00:00Z",
        )
        before = await fetch_claims_as_of_pg(
            conn, registry, {"telegram:owner-a"}, "2026-10-03T01:30:00Z"
        )
        after = await fetch_claims_as_of_pg(
            conn, registry, {"telegram:owner-a"}, "2026-10-03T02:30:00Z"
        )
        other = await fetch_claims_as_of_pg(
            conn, registry, {"telegram:owner-b"}, "2026-10-03T02:30:00Z"
        )
        assert [row["value"] for row in before] == ["a"]
        assert after == []
        assert [row["value"] for row in other] == ["b"]


@pytest.mark.asyncio
async def test_pg_same_object_concurrent_replay_is_idempotent():
    from vera_shared.db.shadow_pg_ingest import PgShadowIngest

    ingest = PgShadowIngest({("telegram", "owner-a"): "telegram:owner-a"})
    async with (
        isolated_shadow_schema() as (engine, schema),
        engine.connect() as one,
        engine.connect() as two,
    ):
        await use_schema(one, schema)
        await use_schema(two, schema)
        results = await asyncio.gather(
            ingest.apply(
                one,
                sample_source(),
                [sample_claim()],
                known_at="2026-10-03T01:00:00Z",
                cursor="42",
            ),
            ingest.apply(
                two,
                sample_source(),
                [sample_claim()],
                known_at="2026-10-03T01:00:00Z",
                cursor="42",
            ),
        )
        assert sorted(results) == [False, True]
        result = await one.execute(text("SELECT count(*) FROM brain_generations"))
        assert result.scalar_one() == 1
