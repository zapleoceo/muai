"""Exercise the real gateway route against migration 047 on isolated PostgreSQL."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from gateway import events as gateway_events
from gateway.shadow_events import ingest_shadow_event
from shadow_pg_support import isolated_migrated_shadow_schema, use_schema
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from vera_shared.events.schema import RawEvent
from vera_shared.ingest.shadow_instagram import (
    read_instagram_shadow,
    resolve_instagram_receipt,
)
from vera_shared.ingest.shadow_types import Quarantine

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION_TESTS") != "1",
    reason="Set RUN_INTEGRATION_TESTS=1 and TEST_DATABASE_URL for isolated PostgreSQL",
)


def message(value: str, revision: int, *, account: str = "owner-a", deleted: bool = False):
    return RawEvent(
        source="instagram",
        source_event_id="ig:thread-1:message-2",
        account=account,
        occurred_at=datetime(2026, 10, 3, tzinfo=UTC),
        content_text=value,
        metadata={
            "thread_id": "thread-1",
            "message_id": "message-2",
            "source_revision": revision,
            "deleted": deleted,
        },
    )


async def gateway_engine(monkeypatch, schema: str):
    engine = create_async_engine(
        os.environ["TEST_DATABASE_URL"],
        connect_args={"server_settings": {"search_path": schema}},
    )
    monkeypatch.setattr(gateway_events, "get_engine", lambda: engine)
    monkeypatch.setattr(gateway_events, "check_internal_secret", lambda _: None)
    monkeypatch.setenv("VERA_SHADOW_INSTAGRAM_ENABLED", "1")
    return engine


async def send(event: RawEvent):
    return await gateway_events.ingest_event("instagram", event, x_internal_secret="test")


def receipt_hash(content: str, metadata: dict, occurred_at: str) -> str:
    payload = {"text": content, "metadata": metadata, "occurred_at": occurred_at}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


@pytest.mark.asyncio
async def test_gateway_ordered_edit_stale_retry_and_tombstone(monkeypatch):
    async with isolated_migrated_shadow_schema() as (engine, schema):
        async with engine.connect() as conn:
            await use_schema(conn, schema)
            await conn.execute(text("INSERT INTO instagram_sessions VALUES ('owner-a',true)"))
            await conn.commit()
        route_engine = await gateway_engine(monkeypatch, schema)
        try:
            first = await send(message("A", 1))
            assert first["deduped"] is False
            assert (await send(message("B", 2)))["deduped"] is True
            with pytest.raises(HTTPException, match="out-of-order|stale"):
                await send(message("A", 1))
            await send(message("", 3, deleted=True))
            with pytest.raises(HTTPException, match="out-of-order|stale"):
                await send(message("B", 2))
            async with engine.connect() as conn:
                await use_schema(conn, schema)
                assert (await conn.execute(text("SELECT content_text FROM events"))).scalar_one() == ""
                assert (await conn.execute(text("SELECT revision FROM brain_revisions ORDER BY revision"))).scalars().all() == ["1", "2", "3"]
                evidence = (await conn.execute(text("SELECT value,evidence_anchor FROM brain_claims ORDER BY generation_id"))).all()
                assert [r.value for r in evidence] == ["A", "B"]
                assert evidence[0].evidence_anchor != evidence[1].evidence_anchor
                assert all(":sha256:" in r.evidence_anchor for r in evidence)
                receipts = (await conn.execute(text(
                    "SELECT revision,content_text,metadata,occurred_at,payload_hash,origin "
                    "FROM brain_revision_receipts ORDER BY revision"
                ))).all()
                assert [r.content_text for r in receipts] == ["A", "B", ""]
                assert all(r.origin == "source" for r in receipts)
                assert all(
                    r.payload_hash == receipt_hash(r.content_text, r.metadata, r.occurred_at)
                    for r in receipts
                )
                assert all(
                    any(r.payload_hash in claim.evidence_anchor for claim in evidence)
                    for r in receipts[:2]
                )
        finally:
            await route_engine.dispose()


@pytest.mark.asyncio
async def test_gateway_bootstraps_existing_original_and_rejects_other_account(monkeypatch):
    async with isolated_migrated_shadow_schema() as (engine, schema):
        async with engine.connect() as conn:
            await use_schema(conn, schema)
            await conn.execute(text("INSERT INTO instagram_sessions VALUES ('owner-a',true),('owner-b',true)"))
            await conn.execute(text("INSERT INTO events(source,source_event_id,account,content_text,metadata) VALUES "
                                    "('instagram','ig:thread-1:message-2','owner-a','original','{}'::jsonb)"))
            await conn.commit()
        route_engine = await gateway_engine(monkeypatch, schema)
        try:
            with pytest.raises(HTTPException, match="another account"):
                await send(message("intrusion", 1, account="owner-b"))
            await send(message("edited", 2))
            with pytest.raises(HTTPException, match="out-of-order|stale"):
                await send(message("original", 1))
            async with engine.connect() as conn:
                await use_schema(conn, schema)
                assert (await conn.execute(text("SELECT account,content_text FROM events"))).one() == ("owner-a", "edited")
                assert (await conn.execute(text("SELECT value FROM brain_claims ORDER BY generation_id"))).scalars().all() == ["original", "edited"]
                assert (await conn.execute(text("SELECT revision FROM brain_revisions ORDER BY revision"))).scalars().all() == ["1", "2"]
                receipts = (await conn.execute(text(
                    "SELECT content_text,metadata,occurred_at,payload_hash,origin "
                    "FROM brain_revision_receipts ORDER BY revision"
                ))).all()
                assert [r.content_text for r in receipts] == ["original", "edited"]
                assert receipts[0].origin == "legacy_snapshot"
                assert receipts[0].metadata["source_revision"] == 1
                assert all(
                    r.payload_hash == receipt_hash(r.content_text, r.metadata, r.occurred_at)
                    for r in receipts
                )
                first_known = (await conn.execute(text(
                    "SELECT known_from FROM brain_generations WHERE revision='1'"
                ))).scalar_one()
                original_read = await read_instagram_shadow(
                    conn, "owner-a", known_at=first_known.isoformat()
                )
                assert original_read[0]["receipt_origin"] == "legacy_snapshot"
                assert original_read[0]["evidence_kind"] == "inference"
                assert (await read_instagram_shadow(
                    conn, "owner-a", known_at="2099-01-01T00:00:00Z"
                ))[0]["receipt_origin"] == "source"
        finally:
            await route_engine.dispose()


@pytest.mark.asyncio
async def test_gateway_ignores_untrusted_receipt_origin_marker(monkeypatch):
    async with isolated_migrated_shadow_schema() as (engine, schema):
        async with engine.connect() as conn:
            await use_schema(conn, schema)
            await conn.execute(text("INSERT INTO instagram_sessions VALUES ('owner-a',true)"))
            await conn.commit()
        route_engine = await gateway_engine(monkeypatch, schema)
        try:
            event = message("A", 1)
            event.metadata["shadow_bootstrap_legacy"] = True
            await send(event)
            async with engine.connect() as conn:
                await use_schema(conn, schema)
                assert (await conn.execute(text(
                    "SELECT origin FROM brain_revision_receipts"
                ))).scalar_one() == "source"
        finally:
            await route_engine.dispose()


@pytest.mark.asyncio
async def test_receipt_resolver_checks_scope_revocation_and_hash(monkeypatch):
    async with isolated_migrated_shadow_schema() as (engine, schema):
        async with engine.connect() as conn:
            await use_schema(conn, schema)
            await conn.execute(text("INSERT INTO instagram_sessions VALUES ('owner-a',true),('owner-b',true)"))
            await conn.commit()
        route_engine = await gateway_engine(monkeypatch, schema)
        try:
            await send(message("A", 1))
            async with engine.connect() as conn:
                await use_schema(conn, schema)
                sid = "ig:thread-1:message-2"
                receipt = await resolve_instagram_receipt(
                    conn, "owner-a", sid, "1", authorized_scopes={"instagram:owner-a"}
                )
                assert receipt["content_text"] == "A"
                assert receipt["payload_hash"] == receipt_hash(
                    receipt["content_text"], receipt["metadata"], receipt["occurred_at"]
                )
                with pytest.raises(Quarantine, match="access denied"):
                    await resolve_instagram_receipt(
                        conn, "owner-a", sid, "1", authorized_scopes={"instagram:owner-b"}
                    )
                with pytest.raises(Quarantine, match="unavailable"):
                    await resolve_instagram_receipt(
                        conn, "owner-b", sid, "1", authorized_scopes={"instagram:owner-b"}
                    )
                await conn.commit()
                await conn.execute(text("UPDATE instagram_sessions SET is_active=false WHERE username='owner-a'"))
                await conn.commit()
                with pytest.raises(Quarantine, match="access denied"):
                    await resolve_instagram_receipt(
                        conn, "owner-a", sid, "1", authorized_scopes={"instagram:owner-a"}
                    )
                await conn.commit()
                await conn.execute(text("UPDATE instagram_sessions SET is_active=true WHERE username='owner-a'"))
                await conn.execute(text(
                    "UPDATE brain_revision_receipts SET payload_hash='corrupt' "
                    "WHERE provider='instagram' AND account_id='owner-a'"
                ))
                await conn.commit()
                with pytest.raises(Quarantine, match="hash mismatch"):
                    await resolve_instagram_receipt(
                        conn, "owner-a", sid, "1", authorized_scopes={"instagram:owner-a"}
                    )
                await conn.commit()
                await conn.execute(text(
                    "UPDATE brain_revision_receipts SET payload_hash=:digest "
                    "WHERE provider='instagram' AND account_id='owner-a'"
                ), {"digest": receipt["payload_hash"]})
                await conn.execute(text(
                    "UPDATE brain_claims SET evidence_anchor='tampered-anchor'"
                ))
                await conn.commit()
                with pytest.raises(Quarantine, match="anchor mismatch"):
                    await read_instagram_shadow(
                        conn, "owner-a", known_at="2099-01-01T00:00:00Z"
                    )
        finally:
            await route_engine.dispose()


@pytest.mark.asyncio
async def test_gateway_revoked_intake_rolls_back_legacy(monkeypatch):
    async with isolated_migrated_shadow_schema() as (engine, schema):
        async with engine.connect() as conn:
            await use_schema(conn, schema)
            await conn.execute(text("INSERT INTO instagram_sessions VALUES ('owner-a',false)"))
            await conn.commit()
        route_engine = await gateway_engine(monkeypatch, schema)
        try:
            with pytest.raises(HTTPException, match="access revoked"):
                await send(message("A", 1))
            async with engine.connect() as conn:
                await use_schema(conn, schema)
                assert (await conn.execute(text("SELECT count(*) FROM events"))).scalar_one() == 0
        finally:
            await route_engine.dispose()


@pytest.mark.parametrize("phase", ["legacy", "shadow", "projection"])
@pytest.mark.asyncio
async def test_gateway_crash_at_each_former_commit_boundary_retries(monkeypatch, phase):
    async with isolated_migrated_shadow_schema() as (engine, schema):
        async with engine.connect() as conn:
            await use_schema(conn, schema)
            await conn.execute(text("INSERT INTO instagram_sessions VALUES ('owner-a',true)"))
            await conn.commit()
        route_engine = await gateway_engine(monkeypatch, schema)
        original = ingest_shadow_event
        if phase == "projection":
            await send(message("A", 1))

        async def crash(conn, event, values):
            def hook(current):
                if current == phase:
                    raise RuntimeError("injected crash")

            return await original(conn, event, values, phase_hook=hook)

        try:
            monkeypatch.setattr(gateway_events, "ingest_shadow_event", crash)
            with pytest.raises(RuntimeError, match="injected crash"):
                await send(message("B", 2) if phase == "projection" else message("A", 1))
            async with engine.connect() as conn:
                await use_schema(conn, schema)
                if phase == "projection":
                    assert (await conn.execute(text("SELECT content_text FROM events"))).scalar_one() == "A"
                    assert (await conn.execute(text("SELECT revision FROM brain_revisions"))).scalars().all() == ["1"]
                else:
                    for table in ("events", "brain_revisions", "brain_revision_receipts", "brain_event_links", "brain_claims"):
                        assert (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one() == 0
            monkeypatch.setattr(gateway_events, "ingest_shadow_event", original)
            await send(message("B", 2) if phase == "projection" else message("A", 1))
        finally:
            await route_engine.dispose()
