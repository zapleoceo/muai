"""Failure boundaries for the disabled synthetic delivery queue on real PostgreSQL."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from pathlib import Path

import pytest
from shadow_pg_support import (
    isolated_migrated_shadow_schema,
    run_migration_sql,
    use_schema,
)
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from vera_shared.db.shadow_pg_ingest import PgShadowIngest
from vera_shared.ingest.shadow_delivery import (
    ACCOUNT,
    PROVIDER,
    SCOPE,
    Delivery,
    _claim,
    capture,
    deliver_once,
)
from vera_shared.ingest.shadow_types import Claim, Quarantine, SourceRevision

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION_TESTS") != "1",
    reason="Set RUN_INTEGRATION_TESTS=1 and TEST_DATABASE_URL for isolated PostgreSQL",
)


def envelope(
    number: int, *, external_id: str = "object-1", previous: str | None = None
) -> Delivery:
    stamp = f"2026-10-03T00:00:{number:02d}Z"
    return Delivery(
        delivery_id=f"delivery-{external_id}-{number}",
        source=SourceRevision(
            provider=PROVIDER,
            account_id=ACCOUNT,
            object_type="message",
            external_id=external_id,
            revision=str(number),
            content_hash=f"hash-{number}",
            received_at=stamp,
            required_scope=SCOPE,
            previous_revision=previous,
        ),
        claims=(
            Claim(
                subject=external_id,
                predicate="source_text",
                value=f"value-{number}",
                valid_from=stamp,
                evidence_anchor=f"synthetic:{external_id}:{number}",
                evidence_kind="document",
                extraction_version="raw-v1",
            ),
        ),
        known_at=stamp,
    )


@pytest.mark.asyncio
async def test_capture_cursor_atomic_replay_and_outage(monkeypatch):
    monkeypatch.setenv("VERA_SHADOW_SYNTHETIC_DELIVERY_ENABLED", "1")
    async with isolated_migrated_shadow_schema() as (engine, schema):  # noqa: SIM117
        async with engine.connect() as conn:
            await use_schema(conn, schema)
            first, second = envelope(1), envelope(2, previous="1")
            assert await capture(
                conn, expected_cursor=None, next_cursor="page-1", deliveries=[first]
            )
            # A lost response after commit produces an exact no-op on replay.
            assert not await capture(
                conn, expected_cursor=None, next_cursor="page-1", deliveries=[first]
            )
            with pytest.raises(Quarantine, match="changed on replay"):
                await capture(
                    conn, expected_cursor=None, next_cursor="page-1", deliveries=[]
                )
            assert await capture(
                conn,
                expected_cursor="page-1",
                next_cursor="page-2",
                deliveries=[second],
            )
            # Both pages remain durable while the worker is offline past any polling window.
            assert (
                await conn.execute(
                    text("SELECT count(*) FROM brain_delivery_envelopes")
                )
            ).scalar_one() == 2
            await conn.rollback()
            assert await deliver_once(conn)
            assert await deliver_once(conn)
            assert not await deliver_once(conn)
            assert (
                await conn.execute(text("SELECT cursor FROM brain_delivery_cursors"))
            ).scalar_one() == "page-2"
            assert (
                await conn.execute(text("SELECT count(*) FROM brain_generations"))
            ).scalar_one() == 2


@pytest.mark.asyncio
async def test_capture_payload_collision_rolls_back_whole_page(monkeypatch):
    monkeypatch.setenv("VERA_SHADOW_SYNTHETIC_DELIVERY_ENABLED", "1")
    async with isolated_migrated_shadow_schema() as (engine, schema):  # noqa: SIM117
        async with engine.connect() as conn:
            await use_schema(conn, schema)
            original = envelope(1)
            await capture(
                conn, expected_cursor=None, next_cursor="p1", deliveries=[original]
            )
            changed = replace(original, known_at="2026-10-04T00:00:00Z")
            with pytest.raises(Quarantine, match="reused"):
                await capture(
                    conn,
                    expected_cursor="p1",
                    next_cursor="p2",
                    deliveries=[envelope(1, external_id="new"), changed],
                )
            assert (
                await conn.execute(text("SELECT cursor FROM brain_delivery_cursors"))
            ).scalar_one() == "p1"
            assert (
                await conn.execute(
                    text("SELECT count(*) FROM brain_delivery_envelopes")
                )
            ).scalar_one() == 1


@pytest.mark.asyncio
async def test_restart_expired_lease_and_lost_delivery_response(monkeypatch):
    monkeypatch.setenv("VERA_SHADOW_SYNTHETIC_DELIVERY_ENABLED", "1")
    async with isolated_migrated_shadow_schema() as (engine, schema):
        async with engine.connect() as conn:
            await use_schema(conn, schema)
            await capture(
                conn, expected_cursor=None, next_cursor="p1", deliveries=[envelope(1)]
            )
            claimed = await _claim(conn, 1)
            assert claimed is not None
            assert not await deliver_once(conn)
            async with conn.begin():
                await conn.execute(
                    text(
                        "UPDATE brain_delivery_envelopes SET lease_until=now()-interval '1 second'"
                    )
                )
        async with engine.connect() as restarted:
            await use_schema(restarted, schema)
            assert await deliver_once(restarted)
            # Simulates a lost response after the apply+ack commit.
            assert not await deliver_once(restarted)
            assert (
                await restarted.execute(text("SELECT count(*) FROM brain_generations"))
            ).scalar_one() == 1
            assert (
                await restarted.execute(
                    text("SELECT status,attempts FROM brain_delivery_envelopes")
                )
            ).one() == ("done", 2)


@pytest.mark.asyncio
async def test_competing_workers_do_not_duplicate_generation(monkeypatch):
    monkeypatch.setenv("VERA_SHADOW_SYNTHETIC_DELIVERY_ENABLED", "1")
    async with isolated_migrated_shadow_schema() as (engine, schema):
        async with engine.connect() as conn:
            await use_schema(conn, schema)
            await capture(
                conn, expected_cursor=None, next_cursor="p1", deliveries=[envelope(1)]
            )

        async def worker():
            async with engine.connect() as conn:
                await use_schema(conn, schema)
                return await deliver_once(conn)

        assert sorted(await asyncio.gather(worker(), worker())) == [False, True]
        async with engine.connect() as conn:
            await use_schema(conn, schema)
            assert (
                await conn.execute(text("SELECT count(*) FROM brain_generations"))
            ).scalar_one() == 1
            assert (
                await conn.execute(text("SELECT status FROM brain_delivery_envelopes"))
            ).scalar_one() == "done"


@pytest.mark.asyncio
async def test_transient_failure_retries_without_advancing_shadow(monkeypatch):
    monkeypatch.setenv("VERA_SHADOW_SYNTHETIC_DELIVERY_ENABLED", "1")
    async with isolated_migrated_shadow_schema() as (engine, schema):  # noqa: SIM117
        async with engine.connect() as conn:
            await use_schema(conn, schema)
            await capture(
                conn, expected_cursor=None, next_cursor="p1", deliveries=[envelope(1)]
            )
            original = PgShadowIngest.apply

            async def fail_once(self, *args, **kwargs):
                monkeypatch.setattr(PgShadowIngest, "apply", original)
                raise RuntimeError("temporary database failure")

            monkeypatch.setattr(PgShadowIngest, "apply", fail_once)
            assert await deliver_once(conn)
            assert not await deliver_once(conn)
            assert (
                await conn.execute(text("SELECT count(*) FROM brain_generations"))
            ).scalar_one() == 0
            await conn.rollback()
            assert (
                await conn.execute(
                    text("SELECT next_attempt_at > now() FROM brain_delivery_envelopes")
                )
            ).scalar_one()
            await conn.rollback()
            async with conn.begin():
                await conn.execute(
                    text(
                        "UPDATE brain_delivery_envelopes SET next_attempt_at=now()-interval '1 second'"
                    )
                )
            assert await deliver_once(conn)
            assert (
                await conn.execute(
                    text("SELECT status,attempts FROM brain_delivery_envelopes")
                )
            ).one() == ("done", 2)


@pytest.mark.asyncio
async def test_poison_quarantine_blocks_later_envelopes(monkeypatch):
    monkeypatch.setenv("VERA_SHADOW_SYNTHETIC_DELIVERY_ENABLED", "1")
    async with isolated_migrated_shadow_schema() as (engine, schema):  # noqa: SIM117
        async with engine.connect() as conn:
            await use_schema(conn, schema)
            poison = replace(envelope(2, previous="1"), known_at="2026-10-02T00:00:00Z")
            await capture(
                conn,
                expected_cursor=None,
                next_cursor="p1",
                deliveries=[poison, envelope(1, external_id="other")],
            )
            assert await deliver_once(conn)
            assert not await deliver_once(conn)
            assert (
                await conn.execute(
                    text("SELECT status FROM brain_delivery_envelopes ORDER BY id")
                )
            ).scalars().all() == ["quarantined", "pending"]
            assert (
                await conn.execute(text("SELECT count(*) FROM brain_generations"))
            ).scalar_one() == 0


@pytest.mark.asyncio
async def test_feature_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("VERA_SHADOW_SYNTHETIC_DELIVERY_ENABLED", raising=False)
    async with isolated_migrated_shadow_schema() as (engine, schema):  # noqa: SIM117
        async with engine.connect() as conn:
            await use_schema(conn, schema)
            with pytest.raises(Quarantine, match="disabled"):
                await capture(
                    conn,
                    expected_cursor=None,
                    next_cursor="p1",
                    deliveries=[envelope(1)],
                )
            with pytest.raises(Quarantine, match="disabled"):
                await deliver_once(conn)


@pytest.mark.asyncio
async def test_migration_048_repeat_constraints_and_disposable_down():
    async with isolated_migrated_shadow_schema() as (engine, schema):
        up = (
            Path(__file__).resolve().parents[2]
            / "infra/migrations/048_shadow_delivery.sql"
        )
        down = (
            Path(__file__).resolve().parents[2]
            / "infra/rollback/048_shadow_delivery.sql"
        )
        async with engine.connect() as conn:
            await use_schema(conn, schema)
            async with conn.begin():
                await run_migration_sql(conn, up)
            with pytest.raises(IntegrityError):
                async with conn.begin():
                    await conn.execute(
                        text(
                            "INSERT INTO brain_delivery_cursors(provider,account_id) "
                            "VALUES ('instagram','live-account')"
                        )
                    )
            async with conn.begin():
                await run_migration_sql(conn, down)
                assert (
                    await conn.execute(
                        text("SELECT to_regclass('brain_delivery_envelopes')")
                    )
                ).scalar_one() is None
