"""Rollback contract against an isolated real PostgreSQL schema."""

from __future__ import annotations

import os

import pytest
from shadow_pg_support import (
    isolated_shadow_schema,
    sample_claim,
    sample_source,
    use_schema,
)
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from vera_shared.db.shadow_pg_ingest import PgShadowIngest
from vera_shared.db.shadow_pg_read import fetch_claims_as_of_pg
from vera_shared.db.shadow_pg_rollback import PgShadowRollback
from vera_shared.ingest.shadow_types import Quarantine

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION_TESTS") != "1",
    reason="Set RUN_INTEGRATION_TESTS=1 and TEST_DATABASE_URL for isolated PostgreSQL",
)

KEY = ("telegram", "owner-a", "message", "42")
REGISTRY = {KEY[:2]: "telegram:owner-a"}
WRITE = {"write:telegram:owner-a"}


@pytest.mark.asyncio
async def test_pg_restore_is_audited_preserves_source_head_and_checkpoint():
    ingest = PgShadowIngest(REGISTRY)
    rollback = PgShadowRollback(REGISTRY)
    async with isolated_shadow_schema() as (engine, schema), engine.connect() as conn:
        await use_schema(conn, schema)
        assert await ingest.apply(
            conn,
            sample_source(),
            [sample_claim()],
            known_at="2026-10-03T01:00:00Z",
            cursor="1",
        )
        assert await ingest.apply(
            conn,
            sample_source("2"),
            [sample_claim("changed")],
            known_at="2026-10-03T02:00:00Z",
            cursor="2",
            expected_cursor="1",
        )
        generations = (
            (await conn.execute(text("SELECT id FROM brain_generations ORDER BY id")))
            .scalars()
            .all()
        )
        await conn.commit()
        restored = await rollback.restore(
            conn,
            KEY,
            target_generation=generations[0],
            expected_active=generations[1],
            known_at="2026-10-03T03:00:00Z",
            reason="review correction",
            authorized_write_scopes=WRITE,
        )
        state = (
            await conn.execute(
                text(
                    "SELECT active_generation,source_head_revision FROM brain_source_objects"
                )
            )
        ).one()
        assert tuple(state) == (restored, "2")
        assert (
            await conn.execute(text("SELECT cursor FROM brain_checkpoints"))
        ).scalar_one() == "2"
        audit = (
            await conn.execute(
                text(
                    "SELECT from_generation,target_generation,restored_generation,reason "
                    "FROM brain_rollback_audit"
                )
            )
        ).one()
        assert tuple(audit) == (
            generations[1],
            generations[0],
            restored,
            "review correction",
        )
        await conn.commit()
        old = await fetch_claims_as_of_pg(
            conn, REGISTRY, {"telegram:owner-a"}, "2026-10-03T02:30:00Z"
        )
        current = await fetch_claims_as_of_pg(
            conn, REGISTRY, {"telegram:owner-a"}, "2026-10-03T03:30:00Z"
        )
        assert [row["value"] for row in old] == ["changed"]
        assert [row["value"] for row in current] == ["ship"]
        assert (
            await fetch_claims_as_of_pg(conn, REGISTRY, set(), "2026-10-03T03:30:00Z")
            == []
        )
        await conn.commit()
        assert await ingest.apply(
            conn,
            sample_source("3"),
            [sample_claim("after restoration")],
            known_at="2026-10-03T04:00:00Z",
            cursor="3",
            expected_cursor="2",
        )


@pytest.mark.asyncio
async def test_pg_restore_rejects_denied_stale_and_wrong_target():
    ingest = PgShadowIngest(REGISTRY)
    rollback = PgShadowRollback(REGISTRY)
    async with isolated_shadow_schema() as (engine, schema), engine.connect() as conn:
        await use_schema(conn, schema)
        await ingest.apply(
            conn, sample_source(), [sample_claim()], known_at="2026-10-03T01:00:00Z"
        )
        await ingest.apply(
            conn,
            sample_source("2"),
            [sample_claim("new")],
            known_at="2026-10-03T02:00:00Z",
        )
        generations = (
            (await conn.execute(text("SELECT id FROM brain_generations ORDER BY id")))
            .scalars()
            .all()
        )
        await conn.commit()
        args = {
            "target_generation": generations[0],
            "expected_active": generations[1],
            "known_at": "2026-10-03T03:00:00Z",
            "reason": "correction",
            "authorized_write_scopes": WRITE,
        }
        with pytest.raises(Quarantine, match="write access denied"):
            await rollback.restore(
                conn, KEY, **(args | {"authorized_write_scopes": set()})
            )
        with pytest.raises(Quarantine, match="active generation changed"):
            await rollback.restore(
                conn, KEY, **(args | {"expected_active": generations[0]})
            )
        with pytest.raises(Quarantine, match="invalid rollback target"):
            await rollback.restore(conn, KEY, **(args | {"target_generation": 999999}))
        with pytest.raises(Quarantine, match="invalid rollback target"):
            await rollback.restore(
                conn, KEY, **(args | {"known_at": "2026-10-03T02:00:00Z"})
            )
        with pytest.raises(Quarantine, match="reason required"):
            await rollback.restore(conn, KEY, **(args | {"reason": "  "}))
        assert (
            await conn.execute(text("SELECT count(*) FROM brain_rollback_audit"))
        ).scalar_one() == 0
        await conn.commit()
        await rollback.restore(conn, KEY, **args)
        with pytest.raises(Quarantine, match="active generation changed"):
            await rollback.restore(conn, KEY, **args)


@pytest.mark.asyncio
async def test_pg_restore_rolls_back_when_audit_insert_fails():
    ingest = PgShadowIngest(REGISTRY)
    rollback = PgShadowRollback(REGISTRY)
    async with isolated_shadow_schema() as (engine, schema), engine.connect() as conn:
        await use_schema(conn, schema)
        await ingest.apply(
            conn, sample_source(), [sample_claim()], known_at="2026-10-03T01:00:00Z"
        )
        await ingest.apply(
            conn,
            sample_source("2"),
            [sample_claim("new")],
            known_at="2026-10-03T02:00:00Z",
        )
        generations = (
            (await conn.execute(text("SELECT id FROM brain_generations ORDER BY id")))
            .scalars()
            .all()
        )
        await conn.execute(text("DROP TABLE brain_rollback_audit"))
        await conn.commit()
        with pytest.raises(DBAPIError, match="brain_rollback_audit"):
            await rollback.restore(
                conn,
                KEY,
                target_generation=generations[0],
                expected_active=generations[1],
                known_at="2026-10-03T03:00:00Z",
                reason="correction",
                authorized_write_scopes=WRITE,
            )
        state = (
            await conn.execute(
                text("SELECT active_generation FROM brain_source_objects")
            )
        ).scalar_one()
        assert state == generations[1]
        assert (
            await conn.execute(text("SELECT count(*) FROM brain_generations"))
        ).scalar_one() == 2
