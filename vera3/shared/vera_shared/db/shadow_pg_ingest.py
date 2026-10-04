"""Atomic PostgreSQL shadow ingest; requires an isolated, explicitly supplied DB."""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from vera_shared.db.shadow_pg_checkpoint import _advance_checkpoint, _lock_checkpoint
from vera_shared.db.shadow_pg_claims import (
    _as_dt,
    _check_generation_claims,
    _insert_generation_claims,
)
from vera_shared.ingest.shadow_types import Claim, Quarantine, SourceRevision, validate_generation


class PgShadowIngest:
    def __init__(self, account_scopes: Mapping[tuple[str, str], str]):
        self.account_scopes = dict(account_scopes)

    async def apply(
        self,
        conn: AsyncConnection,
        source: SourceRevision,
        claims: list[Claim],
        *,
        known_at: str,
        cursor: str | None = None,
        expected_cursor: str | None = None,
    ) -> bool:
        if conn.in_transaction():
            raise Quarantine("ingest requires idle connection")
        received, known, valid_from, valid_to, version = validate_generation(
            source, claims, known_at
        )
        if known < received:
            raise Quarantine("knowledge precedes source receipt")
        if self.account_scopes.get((source.provider, source.account_id)) != source.required_scope:
            raise Quarantine("untrusted source scope")
        key = {
            "provider": source.provider,
            "account_id": source.account_id,
            "object_type": source.object_type,
            "external_id": source.external_id,
        }
        async with conn.begin():
            checkpoint = await _lock_checkpoint(conn, key)
            await conn.execute(
                text(
                    "INSERT INTO brain_source_objects(provider,account_id,object_type,external_id) "
                    "VALUES (:provider,:account_id,:object_type,:external_id) "
                    "ON CONFLICT DO NOTHING"
                ),
                key,
            )
            obj = (
                await conn.execute(
                    text(
                        "SELECT active_generation,source_head_revision FROM brain_source_objects "
                        "WHERE provider=:provider AND account_id=:account_id "
                        "AND object_type=:object_type AND external_id=:external_id FOR UPDATE"
                    ),
                    key,
                )
            ).one()
            old = (
                await conn.execute(
                    text(
                        "SELECT content_hash,required_scope,deleted,received_at,previous_revision "
                        "FROM brain_revisions WHERE provider=:provider AND account_id=:account_id "
                        "AND object_type=:object_type AND external_id=:external_id "
                        "AND revision=:revision"
                    ),
                    key | {"revision": source.revision},
                )
            ).first()
            provenance = (
                source.content_hash,
                source.required_scope,
                source.deleted,
                _as_dt(received),
                source.previous_revision,
            )
            if old is not None and tuple(old) != provenance:
                raise Quarantine("revision provenance changed")
            await conn.execute(
                text(
                    "INSERT INTO brain_revisions(provider,account_id,object_type,external_id,"
                    "revision,content_hash,received_at,required_scope,deleted,previous_revision) "
                    "VALUES (:provider,:account_id,:object_type,:external_id,:revision,:content_hash,"
                    ":received_at,:required_scope,:deleted,:previous_revision) ON CONFLICT DO NOTHING"
                ),
                key
                | {
                    "revision": source.revision,
                    "content_hash": source.content_hash,
                    "received_at": _as_dt(received),
                    "required_scope": source.required_scope,
                    "deleted": source.deleted,
                    "previous_revision": source.previous_revision,
                },
            )
            active = None
            if obj.active_generation is not None:
                active = (
                    await conn.execute(
                        text("SELECT id,known_from FROM brain_generations WHERE id=:id"),
                        {"id": obj.active_generation},
                    )
                ).one()
            if active is not None and _as_dt(known) < active.known_from:
                raise Quarantine("out-of-order knowledge time")
            if obj.source_head_revision is not None and source.revision != obj.source_head_revision:
                head_received = (
                    await conn.execute(
                        text(
                            "SELECT received_at FROM brain_revisions WHERE provider=:provider "
                            "AND account_id=:account_id AND object_type=:object_type "
                            "AND external_id=:external_id AND revision=:revision"
                        ),
                        key | {"revision": obj.source_head_revision},
                    )
                ).scalar_one()
                if (
                    source.previous_revision != obj.source_head_revision
                    or _as_dt(received) <= head_received
                ):
                    raise Quarantine("out-of-order source revision")
            existing = (
                await conn.execute(
                    text(
                        "SELECT id FROM brain_generations WHERE provider=:provider "
                        "AND account_id=:account_id AND object_type=:object_type "
                        "AND external_id=:external_id AND revision=:revision "
                        "AND extraction_version=:version"
                    ),
                    key | {"revision": source.revision, "version": version},
                )
            ).scalar_one_or_none()
            if existing is not None and active is not None and existing != active.id:
                raise Quarantine("stale extraction generation")
            if existing is not None:
                await _check_generation_claims(conn, existing, claims, valid_from, valid_to)
            changed = existing is None
            if changed:
                if active is not None and _as_dt(known) <= active.known_from:
                    raise Quarantine("generation knowledge time must advance")
                if active is not None:
                    await conn.execute(
                        text("UPDATE brain_generations SET known_to=:known WHERE id=:id"),
                        {"known": _as_dt(known), "id": active.id},
                    )
                generation = (
                    await conn.execute(
                        text(
                            "INSERT INTO brain_generations(provider,account_id,object_type,"
                            "external_id,revision,extraction_version,known_from) "
                            "VALUES (:provider,:account_id,:object_type,:external_id,:revision,"
                            ":version,:known) RETURNING id"
                        ),
                        key
                        | {"revision": source.revision, "version": version, "known": _as_dt(known)},
                    )
                ).scalar_one()
                await _insert_generation_claims(conn, generation, claims, valid_from, valid_to)
                await conn.execute(
                    text(
                        "UPDATE brain_source_objects SET active_generation=:id,"
                        "source_head_revision=:revision WHERE provider=:provider "
                        "AND account_id=:account_id AND object_type=:object_type "
                        "AND external_id=:external_id"
                    ),
                    key | {"id": generation, "revision": source.revision},
                )
            await _advance_checkpoint(
                conn, key, current=checkpoint, cursor=cursor, expected_cursor=expected_cursor
            )
            return changed
