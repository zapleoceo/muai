"""Instagram source event through the actual migrated PostgreSQL shadow path."""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from shadow_pg_support import isolated_migrated_shadow_schema, use_schema
from sqlalchemy import text
from vera_shared.events.schema import RawEvent
from vera_shared.ingest.shadow_instagram import (
    ingest_instagram_shadow,
    invalidate_instagram_cache,
    read_instagram_shadow,
    synced_instagram_state,
)
from vera_shared.ingest.shadow_types import Quarantine

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION_TESTS") != "1",
    reason="Set RUN_INTEGRATION_TESTS=1 and TEST_DATABASE_URL for isolated PostgreSQL",
)


def source_event(
    value: str, *, deleted: bool = False, account: str = "owner-a"
) -> RawEvent:
    return RawEvent(
        source="instagram",
        source_event_id="ig:thread-1:message-2",
        account=account,
        occurred_at=datetime(2026, 10, 3, tzinfo=UTC),
        content_text=value,
        metadata={
            "thread_id": "thread-1",
            "message_id": "message-2",
            "deleted": deleted,
        },
    )


@pytest.mark.asyncio
async def test_instagram_source_edit_tombstone_mapping_and_cache():
    async with isolated_migrated_shadow_schema() as (
        engine,
        schema,
    ), engine.connect() as conn:
        await use_schema(conn, schema)
        await conn.execute(
            text("INSERT INTO instagram_sessions VALUES ('owner-a',true)")
        )
        await conn.execute(
            text(
                "INSERT INTO events(id,source,source_event_id,account,content_text,metadata) "
                "VALUES (8123,'instagram','ig:thread-1:message-2','owner-a','first','{}'::jsonb)"
            )
        )
        await conn.commit()

        assert await ingest_instagram_shadow(
            conn, source_event("first"), legacy_event_id=8123
        )
        assert not await ingest_instagram_shadow(
            conn, source_event("first"), legacy_event_id=8123
        )
        assert [
            r["value"]
            for r in await read_instagram_shadow(
                conn, "owner-a", known_at="2099-01-01T00:00:00Z"
            )
        ] == ["first"]
        await conn.commit()

        assert await ingest_instagram_shadow(
            conn, source_event("edited"), legacy_event_id=8123
        )
        assert [
            r["value"]
            for r in await read_instagram_shadow(
                conn, "owner-a", known_at="2099-01-01T00:00:00Z"
            )
        ] == ["edited"]
        link = (
            await conn.execute(text("SELECT event_id FROM brain_event_links"))
        ).scalar_one()
        assert link == 8123
        assert "ig:thread-1:message-2" in await synced_instagram_state(
            conn, "owner-a", ["ig:thread-1:message-2"]
        )
        assert (
            await synced_instagram_state(conn, "owner-b", ["ig:thread-1:message-2"])
            == {}
        )
        revisions = (
            (
                await conn.execute(
                    text("SELECT revision FROM brain_revisions ORDER BY revision")
                )
            )
            .scalars()
            .all()
        )
        assert revisions == ["1", "2"]
        await conn.execute(
            text(
                "INSERT INTO brain_derived_cache VALUES "
                "('instagram','owner-a','old-answer','{}'::jsonb)"
            )
        )
        await conn.commit()

        assert await ingest_instagram_shadow(
            conn, source_event("", deleted=True), legacy_event_id=8123
        )
        assert (
            await read_instagram_shadow(
                conn, "owner-a", known_at="2099-01-01T00:00:00Z"
            )
            == []
        )
        assert (
            await conn.execute(text("SELECT count(*) FROM brain_derived_cache"))
        ).scalar_one() == 0
        assert (
            await conn.execute(text("SELECT event_id FROM brain_event_links"))
        ).scalar_one() == 8123


@pytest.mark.asyncio
async def test_revoked_session_blocks_read_and_ingest_even_with_stale_cache():
    async with isolated_migrated_shadow_schema() as (
        engine,
        schema,
    ), engine.connect() as conn:
        await use_schema(conn, schema)
        await conn.execute(
            text("INSERT INTO instagram_sessions VALUES ('owner-a',true)")
        )
        await conn.execute(text("INSERT INTO events(id) VALUES (8123)"))
        await conn.commit()
        await ingest_instagram_shadow(
            conn, source_event("private"), legacy_event_id=8123
        )
        await conn.execute(
            text(
                "INSERT INTO brain_derived_cache VALUES ('instagram','owner-a','answer','{}'::jsonb)"
            )
        )
        await conn.execute(text("UPDATE instagram_sessions SET is_active=false"))
        await invalidate_instagram_cache(conn)
        await conn.commit()
        assert (
            await read_instagram_shadow(
                conn, "owner-a", known_at="2099-01-01T00:00:00Z"
            )
            == []
        )
        await conn.commit()
        assert (
            await conn.execute(text("SELECT count(*) FROM brain_derived_cache"))
        ).scalar_one() == 0
        await conn.commit()
        with pytest.raises(Quarantine, match="access revoked"):
            await ingest_instagram_shadow(
                conn, source_event("edited"), legacy_event_id=8123
            )
        assert (
            await conn.execute(text("SELECT count(*) FROM brain_revisions"))
        ).scalar_one() == 1


@pytest.mark.asyncio
async def test_instagram_adapter_rejects_spoofed_identity_and_legacy_remap():
    async with isolated_migrated_shadow_schema() as (
        engine,
        schema,
    ), engine.connect() as conn:
        await use_schema(conn, schema)
        await conn.execute(
            text("INSERT INTO instagram_sessions VALUES ('owner-a',true)")
        )
        await conn.execute(text("INSERT INTO events(id) VALUES (8123),(8124)"))
        await conn.commit()
        with pytest.raises(Quarantine, match="untrusted Instagram source identity"):
            await ingest_instagram_shadow(
                conn, source_event("other", account=""), legacy_event_id=8123
            )
        await ingest_instagram_shadow(conn, source_event("first"), legacy_event_id=8123)
        with pytest.raises(Quarantine, match="legacy event identity changed"):
            await ingest_instagram_shadow(
                conn, source_event("first"), legacy_event_id=8124
            )
        assert (
            await conn.execute(text("SELECT event_id FROM brain_event_links"))
        ).scalar_one() == 8123
