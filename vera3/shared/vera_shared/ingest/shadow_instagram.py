"""Opt-in Instagram gateway event to cited shadow revision adapter."""

from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from vera_shared.db.shadow_pg_checkpoint import _lock_checkpoint
from vera_shared.db.shadow_pg_ingest import PgShadowIngest
from vera_shared.db.shadow_pg_read import fetch_claims_as_of_pg
from vera_shared.events.schema import RawEvent
from vera_shared.ingest.shadow_types import Claim, Quarantine, SourceRevision


def _identity(event: RawEvent) -> tuple[str, str]:
    account = event.account
    parts = event.source_event_id.split(":")
    meta = event.metadata or {}
    if (
        event.source != "instagram"
        or not account
        or len(parts) != 3
        or parts[0] != "ig"
        or not parts[1]
        or not parts[2]
        or str(meta.get("thread_id")) != parts[1]
        or str(meta.get("message_id")) != parts[2]
    ):
        raise Quarantine("untrusted Instagram source identity")
    return account, event.source_event_id


def instagram_event_hash(event: RawEvent) -> str:
    payload = {
        "text": event.content_text,
        "metadata": event.metadata,
        "occurred_at": event.occurred_at.isoformat(),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


async def _active_session(conn: AsyncConnection, account: str) -> bool:
    return bool(
        (
            await conn.execute(
                text(
                    "SELECT 1 FROM instagram_sessions WHERE username=:account AND is_active "
                    "LIMIT 1 FOR SHARE"
                ),
                {"account": account},
            )
        ).scalar_one_or_none()
    )


async def ingest_instagram_shadow(
    conn: AsyncConnection, event: RawEvent, *, legacy_event_id: int,
    in_transaction: bool = False,
    receipt_origin: str = "source",
) -> bool:
    """Require the active connector account; preserve the original legacy event ID."""
    if conn.in_transaction() != in_transaction:
        raise Quarantine("shadow adapter transaction state mismatch")
    if receipt_origin not in {"source", "legacy_snapshot"}:
        raise Quarantine("invalid Instagram receipt origin")
    async with (nullcontext() if in_transaction else conn.begin()):
        return await _ingest_in_transaction(conn, event, legacy_event_id, receipt_origin)


async def _ingest_in_transaction(
    conn: AsyncConnection, event: RawEvent, legacy_event_id: int, receipt_origin: str
) -> bool:
    account, external_id = _identity(event)
    await _lock_checkpoint(conn, {"provider": "instagram", "account_id": account})
    if not await _active_session(conn, account):
        raise Quarantine("Instagram account access revoked")
    digest = instagram_event_hash(event)
    deleted = (event.metadata or {}).get("deleted") is True
    head = (
        await conn.execute(
            text(
                "SELECT o.source_head_revision,r.content_hash,r.received_at,r.previous_revision,"
                "g.known_from FROM brain_source_objects o "
                "JOIN brain_revisions r ON (r.provider,r.account_id,r.object_type,"
                "r.external_id,r.revision)=(o.provider,o.account_id,o.object_type,"
                "o.external_id,o.source_head_revision) "
                "JOIN brain_generations g ON g.id=o.active_generation "
                "WHERE o.provider='instagram' AND o.account_id=:account "
                "AND o.object_type='message' AND o.external_id=:external_id"
            ),
            {"account": account, "external_id": external_id},
        )
    ).first()
    source_version = (event.metadata or {}).get("source_revision")
    if source_version is not None and (
        isinstance(source_version, bool)
        or not isinstance(source_version, int)
        or source_version < 1
    ):
        raise Quarantine("invalid Instagram source revision")
    if head is not None and head.content_hash == digest:
        if source_version is not None and str(source_version) != head.source_head_revision:
            raise Quarantine("stale Instagram source revision")
        revision = head.source_head_revision
        previous = head.previous_revision
        received = head.received_at
        known = head.known_from
    else:
        if head is not None and not head.source_head_revision.isdigit():
            raise Quarantine("unsupported Instagram revision chain")
        expected = int(head.source_head_revision) + 1 if head else 1
        if source_version != expected and not (head is None and source_version is None):
            raise Quarantine("missing or out-of-order Instagram source revision")
        revision = str(expected)
        previous = head.source_head_revision if head else None
        now = datetime.now(UTC)
        if head is not None:
            now = max(now, head.received_at + timedelta(microseconds=1))
            now = max(now, head.known_from + timedelta(microseconds=1))
        received = known = now
    scope = f"instagram:{account}"
    source = SourceRevision(
        provider="instagram",
        account_id=account,
        object_type="message",
        external_id=external_id,
        revision=revision,
        content_hash=digest,
        received_at=received.isoformat(),
        required_scope=scope,
        deleted=deleted,
        previous_revision=previous,
    )
    occurred_at = event.occurred_at
    occurred = (
        occurred_at.replace(tzinfo=UTC) if occurred_at.tzinfo is None
        else occurred_at.astimezone(UTC)
    ).isoformat()
    claims = (
        []
        if deleted
        else [
            Claim(
                subject=f"instagram:{account}:{external_id}",
                predicate="source_text",
                value=event.content_text,
                valid_from=occurred,
                evidence_anchor=(
                    f"receipt:instagram:{account}:{external_id}:{revision}:"
                    f"event:{legacy_event_id}:sha256:{digest}"
                ),
                evidence_kind="document",
                extraction_version="raw-v1",
            )
        ]
    )
    changed = await PgShadowIngest({("instagram", account): scope}).apply(
        conn,
        source,
        claims,
        known_at=known.isoformat(),
        legacy_event_id=legacy_event_id,
        active_instagram_account=account,
        in_transaction=True,
    )
    origin = receipt_origin
    receipt = {
        "account": account,
        "external_id": external_id,
        "revision": revision,
        "content_text": event.content_text,
        "metadata": json.dumps(event.metadata, sort_keys=True, default=str),
        "occurred_at": event.occurred_at.isoformat(),
        "payload_hash": digest,
        "origin": origin,
    }
    await conn.execute(
        text(
            "INSERT INTO brain_revision_receipts(provider,account_id,object_type,external_id,"
            "revision,content_text,metadata,occurred_at,payload_hash,origin) "
            "VALUES ('instagram',:account,'message',:external_id,:revision,:content_text,"
            "CAST(:metadata AS jsonb),:occurred_at,:payload_hash,:origin) "
            "ON CONFLICT DO NOTHING"
        ),
        receipt,
    )
    stored = (
        await conn.execute(
            text(
                "SELECT content_text,metadata,occurred_at,payload_hash,origin "
                "FROM brain_revision_receipts WHERE provider='instagram' AND account_id=:account "
                "AND object_type='message' AND external_id=:external_id AND revision=:revision"
            ),
            receipt,
        )
    ).one()
    if tuple(stored) != (
        event.content_text,
        event.metadata,
        event.occurred_at.isoformat(),
        digest,
        origin,
    ):
        raise Quarantine("Instagram revision receipt changed")
    return changed


async def read_instagram_shadow(
    conn: AsyncConnection, account: str, *, known_at: str
) -> list[dict[str, object]]:
    """Use current connector state on every read; never serve a derived cache."""
    if not await _active_session(conn, account):
        return []
    scope = f"instagram:{account}"
    return await fetch_claims_as_of_pg(conn, {("instagram", account): scope}, {scope}, known_at)


async def invalidate_instagram_cache(
    conn: AsyncConnection | AsyncSession, account: str | None = None
) -> None:
    """Remove derived rows when an account is revoked or a source is deleted."""
    exists = (await conn.execute(text("SELECT to_regclass('brain_derived_cache')"))).scalar_one()
    if exists is None:
        return
    if account is None:
        await conn.execute(text("DELETE FROM brain_derived_cache WHERE provider='instagram'"))
    else:
        await conn.execute(
            text(
                "DELETE FROM brain_derived_cache WHERE provider='instagram' AND account_id=:account"
            ),
            {"account": account},
        )


async def synced_instagram_state(
    conn: AsyncConnection | AsyncSession, account: str, sids: list[str]
) -> dict[str, tuple[str, str, dict[str, object] | None]]:
    """Find only this account's linked source revisions and legacy content."""
    if not sids:
        return {}
    statement = text(
        "SELECT e.source_event_id,r.content_hash,e.content_text,e.metadata "
        "FROM events e JOIN brain_event_links l ON l.event_id=e.id "
        "JOIN brain_source_objects o ON (o.provider,o.account_id,o.object_type,o.external_id)="
        "(l.provider,l.account_id,l.object_type,l.external_id) "
        "JOIN brain_revisions r ON (r.provider,r.account_id,r.object_type,r.external_id,r.revision)="
        "(o.provider,o.account_id,o.object_type,o.external_id,o.source_head_revision) "
        "WHERE e.source='instagram' AND e.account=:account "
        "AND l.provider='instagram' AND l.account_id=:account "
        "AND l.object_type='message' AND l.external_id=e.source_event_id "
        "AND e.source_event_id IN :sids"
    ).bindparams(bindparam("sids", expanding=True))
    rows = (await conn.execute(statement, {"account": account, "sids": sids})).all()
    return {sid: (digest, content, metadata) for sid, digest, content, metadata in rows}
