"""PostgreSQL shadow ingestion contract with synthetic data and isolated schema."""

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
async def test_pg_ingest_replay_revision_acl_and_checkpoint_rollback():
    from vera_shared.db.shadow_pg_ingest import PgShadowIngest
    from vera_shared.db.shadow_pg_read import fetch_claims_as_of_pg
    from vera_shared.ingest.shadow_types import Quarantine

    registry = {("telegram", "owner-a"): "telegram:owner-a"}
    ingest = PgShadowIngest(registry)
    async with (
        isolated_shadow_schema() as (engine, schema),
        engine.connect() as conn,
    ):
        await use_schema(conn, schema)
        assert await ingest.apply(
            conn,
            sample_source(),
            [sample_claim()],
            known_at="2026-10-03T01:00:00Z",
            cursor="42",
        )
        assert not await ingest.apply(
            conn,
            sample_source(),
            [sample_claim()],
            known_at="2026-10-03T01:00:00Z",
            cursor="42",
        )
        with pytest.raises(Quarantine, match="revision provenance changed"):
            await ingest.apply(
                conn,
                replace(sample_source(), content_hash="conflict"),
                [sample_claim()],
                known_at="2026-10-03T02:00:00Z",
                cursor="43",
            )
        with pytest.raises(Quarantine, match="untrusted source scope"):
            await ingest.apply(
                conn,
                replace(sample_source(), required_scope="telegram:other"),
                [sample_claim()],
                known_at="2026-10-03T02:00:00Z",
            )
        with pytest.raises(Quarantine, match="extraction version changed"):
            await ingest.apply(
                conn,
                sample_source(),
                [sample_claim("conflicting value")],
                known_at="2026-10-03T02:00:00Z",
            )
        with pytest.raises(Quarantine, match="knowledge time must advance"):
            await ingest.apply(
                conn,
                sample_source("2"),
                [sample_claim("later")],
                known_at="2026-10-03T01:00:00Z",
            )
        await conn.execute(text("SELECT 1"))
        with pytest.raises(Quarantine, match="idle connection"):
            await ingest.apply(
                conn,
                sample_source("2"),
                [sample_claim("later")],
                known_at="2026-10-03T02:00:00Z",
            )
        await conn.commit()
        assert await ingest.apply(
            conn,
            sample_source("2"),
            [sample_claim("later")],
            known_at="2026-10-03T02:00:00Z",
            cursor="43",
            expected_cursor="42",
        )
        with pytest.raises(Quarantine, match="out-of-order source revision"):
            await ingest.apply(
                conn,
                sample_source(),
                [sample_claim()],
                known_at="2026-10-03T03:00:00Z",
                cursor="44",
                expected_cursor="43",
            )
        with pytest.raises(Quarantine, match="checkpoint changed"):
            await ingest.apply(
                conn,
                sample_source("3"),
                [sample_claim("should roll back")],
                known_at="2026-10-03T03:00:00Z",
                cursor="44",
                expected_cursor="42",
            )
        result = await conn.execute(text("SELECT cursor FROM brain_checkpoints"))
        assert result.scalar_one() == "43"
        result = await conn.execute(text("SELECT count(*) FROM brain_generations"))
        assert result.scalar_one() == 2
        await conn.commit()
        before = await fetch_claims_as_of_pg(
            conn, registry, {"telegram:owner-a"}, "2026-10-03T01:30:00Z"
        )
        after = await fetch_claims_as_of_pg(
            conn, registry, {"telegram:owner-a"}, "2026-10-03T02:30:00Z"
        )
        assert [row["value"] for row in before] == ["ship"]
        assert [row["value"] for row in after] == ["later"]
        assert (
            await fetch_claims_as_of_pg(conn, registry, set(), "2026-10-03T02:30:00Z")
            == []
        )
        await conn.commit()
        assert await ingest.apply(
            conn,
            sample_source("3"),
            [sample_claim("retried")],
            known_at="2026-10-03T03:00:00Z",
            cursor="44",
            expected_cursor="43",
        )
        assert not await ingest.apply(
            conn,
            sample_source("3"),
            [sample_claim("retried")],
            known_at="2026-10-03T03:00:00Z",
            cursor="44",
        )


@pytest.mark.asyncio
async def test_pg_concurrent_checkpoint_compare_and_swap():
    from vera_shared.db.shadow_pg_ingest import PgShadowIngest
    from vera_shared.ingest.shadow_types import Quarantine

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
                sample_source(external_id="42"),
                [sample_claim()],
                known_at="2026-10-03T01:00:00Z",
                cursor="42",
            ),
            ingest.apply(
                two,
                sample_source(external_id="43"),
                [sample_claim()],
                known_at="2026-10-03T01:00:00Z",
                cursor="43",
            ),
            return_exceptions=True,
        )
        assert sum(result is True for result in results) == 1
        assert sum(isinstance(result, Quarantine) for result in results) == 1
        result = await one.execute(text("SELECT count(*) FROM brain_generations"))
        assert result.scalar_one() == 1
