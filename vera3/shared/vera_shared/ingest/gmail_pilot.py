"""Offline Gmail history contract for synthetic mailboxes only.

The history position, fetched message version, and observation time are kept
separate. This module has no HTTP client, OAuth grant, or live poller entrypoint.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from vera_shared.db.shadow_pg_ingest import PgShadowIngest
from vera_shared.db.shadow_pg_read import fetch_claims_as_of_pg
from vera_shared.ingest.shadow_types import Claim, Quarantine, SourceRevision, utc_timestamp


def _enabled() -> None:
    if os.environ.get("VERA_SHADOW_GMAIL_PILOT_ENABLED") != "1":
        raise Quarantine("offline Gmail pilot is disabled")


def _sub(value: str) -> str:
    if not value.startswith("synthetic-") or len(value) <= len("synthetic-"):
        raise Quarantine("pilot requires a synthetic verified Google subject")
    return value


def _number(value: str) -> int:
    if not value or not value.isascii() or not value.isdecimal():
        raise Quarantine("invalid Gmail history ID")
    return int(value)


def _idle(conn: AsyncConnection) -> None:
    if conn.in_transaction():
        raise Quarantine("Gmail pilot requires idle connection")


def _plain_message(message: dict[str, object]) -> bool:
    labels = message.get("labels")
    return (
        message.get("draft") is False
        and message.get("attachments") == []
        and isinstance(message.get("text"), str)
        and isinstance(labels, list)
        and all(isinstance(label, str) for label in labels)
    )


async def _account(conn: AsyncConnection, sub: str) -> Row[tuple[Any, ...]]:
    row = (
        await conn.execute(
            text(
                "SELECT captured_cursor,applied_cursor,coverage_break,resync_required "
                "FROM brain_gmail_pilot_accounts "
                "WHERE google_sub=:sub AND is_active FOR UPDATE"
            ),
            {"sub": sub},
        )
    ).one_or_none()
    if row is None:
        raise Quarantine("Gmail pilot account access revoked")
    return row


@dataclass(frozen=True)
class GmailChange:
    history_id: str
    message_id: str
    kind: str

    @property
    def key(self) -> str:
        return f"{self.history_id}:{self.kind}:{self.message_id}"

    def validate(self, start: int, end: int) -> None:
        if (
            not self.message_id
            or self.kind not in {"added", "deleted", "label_added", "label_removed"}
            or not start < _number(self.history_id) <= end
        ):
            raise Quarantine("invalid Gmail history change")


async def register_synthetic_mailbox(
    conn: AsyncConnection, sub: str, display_email: str, initial_history_id: str
) -> None:
    """Fixture registration; the verified subject is never inferred from email."""
    _enabled()
    _idle(conn)
    _sub(sub)
    _number(initial_history_id)
    if not display_email:
        raise Quarantine("display email required")
    async with conn.begin():
        await conn.execute(
            text(
                "INSERT INTO brain_gmail_pilot_accounts(google_sub,display_email,"
                "captured_cursor,applied_cursor) VALUES (:sub,:email,:cursor,:cursor) "
                "ON CONFLICT DO NOTHING"
            ),
            {"sub": sub, "email": display_email, "cursor": initial_history_id},
        )
        row = (
            await conn.execute(
                text(
                    "SELECT display_email,captured_cursor FROM brain_gmail_pilot_accounts "
                    "WHERE google_sub=:sub"
                ),
                {"sub": sub},
            )
        ).one()
        if row.display_email != display_email or row.captured_cursor != initial_history_id:
            raise Quarantine("synthetic mailbox identity changed")


async def capture_history_page(
    conn: AsyncConnection,
    sub: str,
    *,
    page_id: str,
    start_history_id: str,
    end_history_id: str,
    changes: list[GmailChange],
) -> bool:
    """Commit an entire history page and pending fetches before its cursor."""
    _enabled()
    _idle(conn)
    _sub(sub)
    start, end = _number(start_history_id), _number(end_history_id)
    if not page_id or end <= start:
        raise Quarantine("invalid Gmail history page")
    for change in changes:
        change.validate(start, end)
    changes = sorted(
        changes, key=lambda item: (_number(item.history_id), item.message_id, item.kind)
    )
    manifest = [vars(change) for change in changes]
    if len({change.key for change in changes}) != len(changes):
        raise Quarantine("duplicate change within Gmail page")
    if len({(change.history_id, change.message_id) for change in changes}) != len(changes):
        raise Quarantine("ambiguous same-position Gmail message changes")
    async with conn.begin():
        account = await _account(conn, sub)
        stored_page = (
            await conn.execute(
                text(
                    "SELECT start_history_id,end_history_id,manifest FROM brain_gmail_pilot_pages "
                    "WHERE google_sub=:sub AND page_id=:page"
                ),
                {"sub": sub, "page": page_id},
            )
        ).one_or_none()
        if stored_page is not None:
            if (stored_page.start_history_id, stored_page.end_history_id, stored_page.manifest) != (
                start_history_id,
                end_history_id,
                manifest,
            ):
                raise Quarantine("Gmail page changed on replay")
            return False
        current = _number(account.captured_cursor)
        if account.resync_required or start > current or end <= current:
            raise Quarantine("Gmail history coverage gap or stale page")
        await conn.execute(
            text(
                "INSERT INTO brain_gmail_pilot_pages(google_sub,page_id,start_history_id,"
                "end_history_id,manifest) VALUES (:sub,:page,:start,:end,CAST(:manifest AS jsonb))"
            ),
            {
                "sub": sub,
                "page": page_id,
                "start": start_history_id,
                "end": end_history_id,
                "manifest": json.dumps(manifest, sort_keys=True),
            },
        )
        for change in changes:
            params = {
                "sub": sub,
                "key": change.key,
                "history": change.history_id,
                "message": change.message_id,
                "kind": change.kind,
            }
            if _number(change.history_id) <= current:
                exists = (
                    await conn.execute(
                        text(
                            "SELECT 1 FROM brain_gmail_pilot_events WHERE google_sub=:sub "
                            "AND event_key=:key"
                        ),
                        params,
                    )
                ).scalar_one_or_none()
                if exists is None:
                    raise Quarantine("new Gmail change appeared behind captured cursor")
            await conn.execute(
                text(
                    "INSERT INTO brain_gmail_pilot_events(google_sub,event_key,history_id,"
                    "message_id,kind) VALUES (:sub,:key,:history,:message,:kind) "
                    "ON CONFLICT DO NOTHING"
                ),
                params,
            )
            stored = (
                await conn.execute(
                    text(
                        "SELECT history_id,message_id,kind FROM brain_gmail_pilot_events "
                        "WHERE google_sub=:sub AND event_key=:key"
                    ),
                    params,
                )
            ).one()
            if tuple(stored) != (change.history_id, change.message_id, change.kind):
                raise Quarantine("Gmail overlapping change identity changed")
        await conn.execute(
            text(
                "UPDATE brain_gmail_pilot_accounts SET captured_cursor=:end WHERE google_sub=:sub"
            ),
            {"sub": sub, "end": end_history_id},
        )
        await _advance_applied(conn, sub)
    return True


async def record_fetch_result(
    conn: AsyncConnection,
    sub: str,
    change: GmailChange,
    *,
    verified_sub: str,
    outcome: str,
    message: dict[str, object] | None = None,
    observed_at: str | None = None,
) -> None:
    """Persist a synthetic GET outcome without pretending it was observed at H."""
    _enabled()
    _idle(conn)
    if verified_sub != _sub(sub):
        raise Quarantine("verified Google subject mismatch")
    if outcome not in {"fetched", "failed", "missing", "forbidden", "deleted_before_fetch"}:
        raise Quarantine("invalid Gmail fetch outcome")
    observed = datetime.fromisoformat(utc_timestamp(observed_at)) if observed_at else None
    if outcome == "fetched":
        if not message or not observed or message.get("id") != change.message_id:
            raise Quarantine("fetched Gmail message identity mismatch")
        version = str(message.get("historyId", ""))
        if _number(version) < _number(change.history_id):
            raise Quarantine("fetched Gmail version precedes history event")
        if not _plain_message(message):
            raise Quarantine("pilot accepts only plain-text non-draft messages")
    else:
        if message is not None:
            raise Quarantine("non-fetched outcome cannot contain message body")
        version = None
    async with conn.begin():
        await _account(conn, sub)
        row = (
            await conn.execute(
                text(
                    "SELECT id,fetch_state,history_id,message_id,kind,fetched_payload,"
                    "observed_history_id,observed_at FROM brain_gmail_pilot_events "
                    "WHERE google_sub=:sub AND event_key=:key FOR UPDATE"
                ),
                {"sub": sub, "key": change.key},
            )
        ).one_or_none()
        if row is None or (row.history_id, row.message_id, row.kind) != (
            change.history_id,
            change.message_id,
            change.kind,
        ):
            raise Quarantine("uncaptured Gmail change")
        if row.fetch_state in {"done", "obsolete", "quarantined", "gap"}:
            raise Quarantine("Gmail fetch result arrived after resolution")
        if row.fetch_state == "fetched":
            if row.fetched_payload != message or row.observed_history_id != version:
                raise Quarantine("Gmail fetched payload changed")
            return
        if outcome == "deleted_before_fetch":
            later_delete = (
                await conn.execute(
                    text(
                        "SELECT 1 FROM brain_gmail_pilot_events WHERE google_sub=:sub "
                        "AND message_id=:message AND kind='deleted' "
                        "AND history_id::numeric > :history LIMIT 1"
                    ),
                    {
                        "sub": sub,
                        "message": change.message_id,
                        "history": _number(change.history_id),
                    },
                )
            ).scalar_one_or_none()
            if not later_delete:
                raise Quarantine("deletion before fetch lacks captured delete event")
        await conn.execute(
            text(
                "UPDATE brain_gmail_pilot_events SET fetch_state=:state,"
                "fetched_payload=CAST(:payload AS jsonb),observed_history_id=:version,"
                "observed_at=:observed,error=:error WHERE id=:id"
            ),
            {
                "id": row.id,
                "state": outcome,
                "payload": json.dumps(message, sort_keys=True) if message else None,
                "version": version,
                "observed": observed,
                "error": outcome if outcome != "fetched" else None,
            },
        )


async def record_history_404(conn: AsyncConnection, sub: str) -> None:
    """Mark an unrecoverable history gap; do not invent intervening events."""
    _enabled()
    _idle(conn)
    async with conn.begin():
        await _account(conn, _sub(sub))
        await conn.execute(
            text(
                "UPDATE brain_gmail_pilot_accounts SET coverage_break=true,"
                "resync_required=true "
                "WHERE google_sub=:sub"
            ),
            {"sub": sub},
        )


async def capture_present_resync(
    conn: AsyncConnection,
    sub: str,
    present_history_id: str,
    messages: list[dict[str, object]],
    observed_at: str,
) -> None:
    """Archive a present-state snapshot after 404, without closing its history gap."""
    _enabled()
    _idle(conn)
    _number(present_history_id)
    observed = datetime.fromisoformat(utc_timestamp(observed_at))
    async with conn.begin():
        account = await _account(conn, _sub(sub))
        if not account.resync_required or _number(present_history_id) <= _number(
            account.captured_cursor
        ):
            raise Quarantine("present-state resync needs a newer coverage break")
        latest_observation = (
            await conn.execute(
                text(
                    "SELECT max(observed_at) FROM brain_gmail_pilot_resyncs WHERE google_sub=:sub"
                ),
                {"sub": sub},
            )
        ).scalar_one()
        latest_generation = (
            await conn.execute(
                text(
                    "SELECT max(known_from) FROM brain_generations "
                    "WHERE provider='gmail-pilot' AND account_id=:sub"
                ),
                {"sub": sub},
            )
        ).scalar_one()
        if any(
            boundary is not None and observed <= boundary
            for boundary in (latest_observation, latest_generation)
        ):
            raise Quarantine("present-state observation time must advance")
        await conn.execute(
            text(
                "UPDATE brain_gmail_pilot_events SET fetch_state='gap',"
                "error='history expired before resolution',applied_at=now() "
                "WHERE google_sub=:sub AND fetch_state NOT IN ('done','obsolete','gap')"
            ),
            {"sub": sub},
        )
        present_ids: set[str] = set()
        for message in messages:
            mid = str(message.get("id", ""))
            version = str(message.get("historyId", ""))
            if _number(version) > _number(present_history_id):
                raise Quarantine("present-state version exceeds mailbox snapshot")
            if not mid or mid in present_ids or not _plain_message(message):
                raise Quarantine("invalid present-state Gmail message")
            present_ids.add(mid)
            await conn.execute(
                text(
                    "INSERT INTO brain_gmail_pilot_events(google_sub,event_key,resync_history_id,"
                    "history_id,message_id,kind,fetch_state,fetched_payload,"
                    "observed_history_id,observed_at) "
                    "VALUES (:sub,:key,:history,:history,:mid,'added','fetched',"
                    "CAST(:payload AS jsonb),"
                    ":version,:observed) ON CONFLICT DO NOTHING"
                ),
                {
                    "sub": sub,
                    "key": f"resync:{present_history_id}:{mid}",
                    "history": present_history_id,
                    "mid": mid,
                    "payload": json.dumps(message, sort_keys=True),
                    "version": version,
                    "observed": observed,
                },
            )
        await conn.execute(
            text(
                "INSERT INTO brain_gmail_pilot_resyncs(google_sub,history_id,"
                "observed_at,present_ids) VALUES (:sub,:history,:observed,"
                "CAST(:present_ids AS jsonb))"
            ),
            {
                "sub": sub,
                "history": present_history_id,
                "observed": observed,
                "present_ids": json.dumps(sorted(present_ids)),
            },
        )
        await conn.execute(
            text(
                "UPDATE brain_gmail_pilot_accounts SET captured_cursor=:cursor,"
                "resync_required=false "
                "WHERE google_sub=:sub"
            ),
            {"sub": sub, "cursor": present_history_id},
        )
        await _advance_applied(conn, sub)


async def _advance_applied(conn: AsyncConnection, sub: str) -> None:
    unresolved = (
        await conn.execute(
            text(
                "SELECT min(history_id::numeric) FROM brain_gmail_pilot_events "
                "WHERE google_sub=:sub AND fetch_state NOT IN ('done','obsolete','gap')"
            ),
            {"sub": sub},
        )
    ).scalar_one()
    if unresolved is None:
        candidate = (
            await conn.execute(
                text(
                    "SELECT captured_cursor FROM brain_gmail_pilot_accounts WHERE google_sub=:sub"
                ),
                {"sub": sub},
            )
        ).scalar_one()
    else:
        candidate = (
            await conn.execute(
                text(
                    "SELECT max(history_id::numeric) FROM brain_gmail_pilot_events "
                    "WHERE google_sub=:sub AND fetch_state IN ('done','obsolete','gap') "
                    "AND history_id::numeric < :unresolved"
                ),
                {"sub": sub, "unresolved": unresolved},
            )
        ).scalar_one()
        candidate = str(int(candidate)) if candidate is not None else None
    if candidate is not None:
        await conn.execute(
            text(
                "UPDATE brain_gmail_pilot_accounts SET applied_cursor=:cursor "
                "WHERE google_sub=:sub AND applied_cursor::numeric <= :cursor_num"
            ),
            {"sub": sub, "cursor": candidate, "cursor_num": _number(candidate)},
        )


async def apply_next(conn: AsyncConnection, sub: str) -> bool:
    """Apply one ready synthetic event and acknowledge it in the same transaction."""
    _enabled()
    _idle(conn)
    _sub(sub)
    selected: list[int] = []
    try:
        return await _apply_next_transaction(conn, sub, selected)
    except Quarantine as exc:
        if not selected:
            raise
        async with conn.begin():
            await _account(conn, sub)
            await conn.execute(
                text(
                    "UPDATE brain_gmail_pilot_events SET fetch_state='quarantined',"
                    "error=:error WHERE id=:id AND fetch_state NOT IN ('done','obsolete','gap')"
                ),
                {"id": selected[0], "error": str(exc)[:1000]},
            )
        return True


async def _apply_next_transaction(conn: AsyncConnection, sub: str, selected: list[int]) -> bool:
    async with conn.begin():
        await _account(conn, sub)
        row = (
            await conn.execute(
                text(
                    "SELECT id,history_id,message_id,kind,resync_history_id,"
                    "fetch_state,fetched_payload,"
                    "observed_history_id,observed_at,captured_at FROM brain_gmail_pilot_events "
                    "WHERE google_sub=:sub AND fetch_state NOT IN ('done','obsolete','gap') "
                    "ORDER BY id LIMIT 1 FOR UPDATE"
                ),
                {"sub": sub},
            )
        ).one_or_none()
        if row is not None:
            selected.append(row.id)
        if row is None or (
            row.kind != "deleted"
            and row.fetch_state in {"pending", "failed", "missing", "forbidden", "quarantined"}
        ):
            return False
        if row.fetch_state == "deleted_before_fetch":
            await conn.execute(
                text(
                    "UPDATE brain_gmail_pilot_accounts SET coverage_break=true "
                    "WHERE google_sub=:sub"
                ),
                {"sub": sub},
            )
        elif row.kind == "deleted":
            await _apply_delete(conn, sub, row)
        elif row.fetch_state == "fetched":
            if not await _apply_fetched(conn, sub, row):
                await conn.execute(
                    text(
                        "UPDATE brain_gmail_pilot_events SET "
                        "fetch_state='obsolete',applied_at=now() WHERE id=:id"
                    ),
                    {"id": row.id},
                )
                await _advance_applied(conn, sub)
                return True
        else:
            raise Quarantine("unexpected Gmail pilot state")
        await conn.execute(
            text(
                "UPDATE brain_gmail_pilot_events SET fetch_state='done',"
                "applied_at=now() WHERE id=:id"
            ),
            {"id": row.id},
        )
        await _advance_applied(conn, sub)
    return True


async def _head(conn: AsyncConnection, sub: str, message_id: str) -> Row[tuple[Any, ...]] | None:
    return (
        await conn.execute(
            text(
                "SELECT o.source_head_revision,r.received_at,r.content_hash "
                "FROM brain_source_objects o "
                "JOIN brain_revisions r ON (r.provider,r.account_id,r.object_type,"
                "r.external_id,r.revision)=(o.provider,o.account_id,o.object_type,"
                "o.external_id,o.source_head_revision) WHERE o.provider='gmail-pilot' "
                "AND o.account_id=:sub AND o.object_type='message' AND o.external_id=:mid"
            ),
            {"sub": sub, "mid": message_id},
        )
    ).one_or_none()


async def _apply_fetched(conn: AsyncConnection, sub: str, row: Row[tuple[Any, ...]]) -> bool:
    version = str(row.observed_history_id)
    head = await _head(conn, sub, row.message_id)
    payload = row.fetched_payload
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    if head and _number(version) <= _number(head.source_head_revision):
        if row.resync_history_id is not None:
            if version != head.source_head_revision or digest != head.content_hash:
                raise Quarantine("present-state payload disagrees with source head")
            return True
        return False  # A later fetch of an older version cannot replace the current body.
    observed = row.observed_at.astimezone(UTC).isoformat()
    if head and row.observed_at <= head.received_at:
        raise Quarantine("Gmail observation time precedes prior observation")
    source = SourceRevision(
        provider="gmail-pilot",
        account_id=sub,
        object_type="message",
        external_id=row.message_id,
        revision=version,
        content_hash=digest,
        received_at=observed,
        required_scope=f"gmail-pilot:{sub}",
        previous_revision=head.source_head_revision if head else None,
    )
    claim = Claim(
        subject=f"gmail:{sub}:{row.message_id}",
        predicate="source_text",
        value=str(payload["text"]),
        valid_from=observed,
        evidence_anchor=f"gmail-observation:{sub}:{row.message_id}:{version}:sha256:{digest}",
        evidence_kind="document",
        extraction_version="plain-v1",
    )
    await PgShadowIngest({("gmail-pilot", sub): source.required_scope}).apply(
        conn, source, [claim], known_at=observed, in_transaction=True
    )
    return True


async def _apply_delete(conn: AsyncConnection, sub: str, row: Row[tuple[Any, ...]]) -> None:
    head = await _head(conn, sub, row.message_id)
    if head is None or _number(row.history_id) <= _number(head.source_head_revision):
        return
    observed = datetime.now(UTC).isoformat()
    if datetime.fromisoformat(observed) <= head.received_at:
        raise Quarantine("Gmail delete observation precedes prior observation")
    source = SourceRevision(
        provider="gmail-pilot",
        account_id=sub,
        object_type="message",
        external_id=row.message_id,
        revision=row.history_id,
        content_hash=f"delete:{row.history_id}",
        received_at=observed,
        required_scope=f"gmail-pilot:{sub}",
        deleted=True,
        previous_revision=head.source_head_revision,
    )
    await PgShadowIngest({("gmail-pilot", sub): source.required_scope}).apply(
        conn, source, [], known_at=observed, in_transaction=True
    )


async def pilot_status(conn: AsyncConnection, sub: str) -> dict[str, object]:
    """Report backlog, quarantine, coverage gap, and separate cursors."""
    _enabled()
    _sub(sub)
    account = await _account(conn, sub)
    counts = (
        await conn.execute(
            text(
                "SELECT fetch_state,count(*) FROM brain_gmail_pilot_events "
                "WHERE google_sub=:sub GROUP BY fetch_state"
            ),
            {"sub": sub},
        )
    ).all()
    return {
        "captured_cursor": account.captured_cursor,
        "applied_cursor": account.applied_cursor,
        "coverage_break": account.coverage_break,
        "resync_required": account.resync_required,
        "counts": {item[0]: item[1] for item in counts},
    }


async def read_pilot(
    conn: AsyncConnection, sub: str, *, verified_sub: str, known_at: str
) -> list[dict[str, object]]:
    """Recheck live synthetic rights on every read; no cached claims."""
    _enabled()
    if verified_sub != _sub(sub):
        raise Quarantine("verified Google subject mismatch")
    await _account(conn, sub)
    scope = f"gmail-pilot:{sub}"
    claims = await fetch_claims_as_of_pg(conn, {("gmail-pilot", sub): scope}, {scope}, known_at)
    resync = (
        await conn.execute(
            text(
                "SELECT history_id,observed_at,present_ids FROM brain_gmail_pilot_resyncs "
                "WHERE google_sub=:sub AND observed_at<=:as_of "
                "ORDER BY observed_at DESC LIMIT 1"
            ),
            {"sub": sub, "as_of": datetime.fromisoformat(utc_timestamp(known_at))},
        )
    ).one_or_none()
    if resync is None:
        return claims
    present = set(resync.present_ids)
    verified = set(
        (
            await conn.execute(
                text(
                    "SELECT message_id FROM brain_gmail_pilot_events "
                    "WHERE google_sub=:sub AND resync_history_id=:history "
                    "AND fetch_state='done'"
                ),
                {"sub": sub, "history": resync.history_id},
            )
        )
        .scalars()
        .all()
    )
    return [
        claim
        for claim in claims
        if claim["known_from"] >= resync.observed_at
        or (claim["external_id"] in present and claim["external_id"] in verified)
    ]
