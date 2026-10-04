"""Read queries for the offline shadow repository."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping

from vera_shared.ingest.shadow_types import Claim, Quarantine


def generation_matches(
    db: sqlite3.Connection,
    generation_id: int,
    claims: list[Claim],
    valid_from: list[str],
    valid_to: list[str | None],
) -> bool:
    stored = db.execute(
        "SELECT subject,predicate,value,valid_from,valid_to,evidence_anchor,evidence_kind "
        "FROM claims WHERE generation_id=? ORDER BY ordinal",
        (generation_id,),
    ).fetchall()
    expected = [
        (
            c.subject,
            c.predicate,
            c.value,
            valid_from[i],
            valid_to[i],
            c.evidence_anchor,
            c.evidence_kind,
        )
        for i, c in enumerate(claims)
    ]
    return stored == expected


def fetch_checkpoint(db: sqlite3.Connection, provider: str, account_id: str) -> str | None:
    row = db.execute(
        "SELECT cursor FROM checkpoints WHERE provider=? AND account_id=?",
        (provider, account_id),
    ).fetchone()
    return row[0] if row else None


def advance_checkpoint(
    db: sqlite3.Connection,
    provider: str,
    account_id: str,
    cursor: str,
    expected_cursor: str | None,
) -> None:
    current = fetch_checkpoint(db, provider, account_id)
    if current != cursor and current != expected_cursor:
        raise Quarantine("checkpoint changed concurrently")
    db.execute(
        "INSERT INTO checkpoints VALUES (?,?,?) ON CONFLICT(provider,account_id) "
        "DO UPDATE SET cursor=excluded.cursor",
        (provider, account_id, cursor),
    )


def fetch_claims(
    db: sqlite3.Connection,
    account_scopes: Mapping[tuple[str, str], str],
    authorized_scopes: set[str],
    *,
    as_of: str | None = None,
) -> list[dict[str, str]]:
    allowed = sorted(
        (provider, account, scope)
        for (provider, account), scope in account_scopes.items()
        if scope in authorized_scopes
    )
    if not allowed:
        return []
    current_query = (
        "SELECT g.provider,g.account_id,g.object_type,g.external_id,g.revision,"
        "g.known_from,c.subject,c.predicate,c.value,c.valid_from,c.valid_to,"
        "c.evidence_anchor,c.evidence_kind,? AS required_scope FROM objects o "
        "JOIN generations g ON g.id=o.active_generation "
        "JOIN revisions r ON (r.provider,r.account_id,r.object_type,"
        "r.external_id,r.revision)=(g.provider,g.account_id,g.object_type,"
        "g.external_id,g.revision) JOIN claims c ON c.generation_id=g.id "
        "WHERE r.deleted=0 AND g.provider=? AND g.account_id=? "
        "ORDER BY g.id,c.ordinal"
    )
    historical_query = (
        "SELECT g.provider,g.account_id,g.object_type,g.external_id,g.revision,"
        "g.known_from,c.subject,c.predicate,c.value,c.valid_from,c.valid_to,"
        "c.evidence_anchor,c.evidence_kind,? AS required_scope FROM objects o "
        "JOIN generations g ON (g.provider,g.account_id,g.object_type,g.external_id)="
        "(o.provider,o.account_id,o.object_type,o.external_id) "
        "JOIN revisions r ON (r.provider,r.account_id,r.object_type,"
        "r.external_id,r.revision)=(g.provider,g.account_id,g.object_type,"
        "g.external_id,g.revision) JOIN claims c ON c.generation_id=g.id "
        "WHERE r.deleted=0 AND g.provider=? AND g.account_id=? "
        "AND g.known_from<=? AND (g.known_to IS NULL OR g.known_to>?) "
        "ORDER BY g.id,c.ordinal"
    )
    query = current_query if as_of is None else historical_query
    rows = [
        row
        for provider, account, scope in allowed
        for row in db.execute(
            query,
            (scope, provider, account)
            if as_of is None
            else (scope, provider, account, as_of, as_of),
        ).fetchall()
    ]
    columns = (
        "provider",
        "account_id",
        "object_type",
        "external_id",
        "revision",
        "known_from",
        "subject",
        "predicate",
        "value",
        "valid_from",
        "valid_to",
        "evidence_anchor",
        "evidence_kind",
        "required_scope",
    )
    return [dict(zip(columns, row, strict=True)) for row in rows]


def fetch_generation_ids(db: sqlite3.Connection, key: tuple[str, str, str, str]) -> list[int]:
    return [
        row[0]
        for row in db.execute(
            "SELECT id FROM generations WHERE provider=? AND account_id=? "
            "AND object_type=? AND external_id=? ORDER BY id",
            key,
        ).fetchall()
    ]
