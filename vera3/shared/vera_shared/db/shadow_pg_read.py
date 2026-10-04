"""PostgreSQL as-of read adapter for the proposed, not yet deployed schema."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from vera_shared.ingest.shadow_types import utc_timestamp

_AS_OF = text(
    "SELECT g.provider,g.account_id,g.object_type,g.external_id,g.revision,"
    "g.known_from,c.subject,c.predicate,c.value,c.valid_from,c.valid_to,"
    "c.evidence_anchor,c.evidence_kind FROM brain_generations g "
    "JOIN brain_source_objects o ON (o.provider,o.account_id,o.object_type,o.external_id)="
    "(g.provider,g.account_id,g.object_type,g.external_id) "
    "JOIN brain_revisions r ON (r.provider,r.account_id,r.object_type,r.external_id,r.revision)="
    "(g.provider,g.account_id,g.object_type,g.external_id,g.revision) "
    "JOIN brain_claims c ON c.generation_id=g.id "
    "WHERE g.provider=:provider AND g.account_id=:account_id AND NOT r.deleted "
    "AND g.known_from<=:as_of AND (g.known_to IS NULL OR g.known_to>:as_of) "
    "ORDER BY g.id,c.ordinal"
)


async def fetch_claims_as_of_pg(
    conn: AsyncConnection,
    account_scopes: Mapping[tuple[str, str], str],
    authorized_scopes: set[str],
    known_at: str,
) -> list[dict[str, object]]:
    """Apply current account grants before reading historical source claims."""
    as_of = datetime.fromisoformat(utc_timestamp(known_at))
    rows: list[dict[str, object]] = []
    for (provider, account_id), scope in sorted(account_scopes.items()):
        if scope not in authorized_scopes:
            continue
        result = await conn.execute(
            _AS_OF, {"provider": provider, "account_id": account_id, "as_of": as_of}
        )
        rows.extend(dict(row) | {"required_scope": scope} for row in result.mappings())
    return rows
