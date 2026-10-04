"""Offline shadow repository from a source revision to a cited claim.

This SQLite model is deliberately disconnected from production ingestion. It
tests source identity, revision replay and access before a PostgreSQL migration.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path

from vera_shared.db.shadow_history import ShadowHistory
from vera_shared.db.shadow_queries import (
    advance_checkpoint,
    fetch_checkpoint,
    generation_matches,
)
from vera_shared.db.shadow_schema import SCHEMA
from vera_shared.ingest.shadow_types import (
    Claim,
    Quarantine,
    SourceRevision,
    validate_generation,
)


class ShadowBrain(ShadowHistory):
    @classmethod
    async def create(
        cls, path: str | Path, *, account_scopes: Mapping[tuple[str, str], str]
    ) -> ShadowBrain:
        brain = cls(path, account_scopes)
        await asyncio.to_thread(brain._init_db)
        return brain

    def _init_db(self) -> None:
        with self._connect() as db:
            db.executescript(SCHEMA)

    async def apply(
        self,
        source: SourceRevision,
        claims: list[Claim],
        *,
        known_at: str,
        cursor: str | None = None,
        expected_cursor: str | None = None,
    ) -> bool:
        return await asyncio.to_thread(
            self._apply,
            source,
            claims,
            known_at=known_at,
            cursor=cursor,
            expected_cursor=expected_cursor,
        )

    def _apply(
        self,
        source: SourceRevision,
        claims: list[Claim],
        *,
        known_at: str,
        cursor: str | None = None,
        expected_cursor: str | None = None,
    ) -> bool:
        key = source.key
        received_at, known_at, valid_from, valid_to, version = validate_generation(
            source, claims, known_at
        )
        if known_at < received_at:
            raise Quarantine("knowledge precedes source receipt")
        if self.account_scopes.get((source.provider, source.account_id)) != source.required_scope:
            raise Quarantine("untrusted source scope")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO objects VALUES (?, ?, ?, ?, NULL, NULL)", key)
            old = db.execute(
                "SELECT content_hash, required_scope, deleted, received_at, "
                "previous_revision FROM revisions "
                "WHERE provider=? AND account_id=? "
                "AND object_type=? AND external_id=? AND revision=?",
                (*key, source.revision),
            ).fetchone()
            if old and old != (
                source.content_hash,
                source.required_scope,
                int(source.deleted),
                received_at,
                source.previous_revision,
            ):
                raise Quarantine("revision provenance changed")
            db.execute(
                "INSERT OR IGNORE INTO revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    *key,
                    source.revision,
                    source.content_hash,
                    received_at,
                    source.required_scope,
                    int(source.deleted),
                    source.previous_revision,
                ),
            )
            active = db.execute(
                "SELECT g.id, g.revision, g.known_from, r.received_at "
                "FROM objects o JOIN generations g ON g.id=o.active_generation "
                "JOIN revisions r ON (r.provider,r.account_id,r.object_type,"
                "r.external_id,r.revision)=(g.provider,g.account_id,g.object_type,"
                "g.external_id,g.revision) WHERE o.provider=? AND o.account_id=? "
                "AND o.object_type=? AND o.external_id=?",
                key,
            ).fetchone()
            head = db.execute(
                "SELECT o.source_head_revision,r.received_at FROM objects o "
                "LEFT JOIN revisions r ON (r.provider,r.account_id,r.object_type,"
                "r.external_id,r.revision)=(o.provider,o.account_id,o.object_type,"
                "o.external_id,o.source_head_revision) WHERE o.provider=? AND o.account_id=? "
                "AND o.object_type=? AND o.external_id=?",
                key,
            ).fetchone()
            if active and (known_at < active[2]):
                raise Quarantine("out-of-order revision or knowledge time")
            if (
                head
                and head[0] is not None
                and source.revision != head[0]
                and (source.previous_revision != head[0] or received_at <= head[1])
            ):
                raise Quarantine("out-of-order source revision")
            existing = db.execute(
                "SELECT id FROM generations WHERE provider=? AND account_id=? "
                "AND object_type=? AND external_id=? AND revision=? "
                "AND extraction_version=?",
                (*key, source.revision, version),
            ).fetchone()
            if existing and active and existing[0] != active[0]:
                raise Quarantine("stale extraction generation")
            if existing and not generation_matches(db, existing[0], claims, valid_from, valid_to):
                raise Quarantine("extraction version changed")
            changed = existing is None
            if changed:
                if active and known_at <= active[2]:
                    raise Quarantine("generation knowledge time must advance")
                if active:
                    db.execute(
                        "UPDATE generations SET known_to=? WHERE id=?",
                        (known_at, active[0]),
                    )
                generation = db.execute(
                    "INSERT INTO generations(provider,account_id,object_type,external_id,"
                    "revision,extraction_version,known_from) VALUES (?,?,?,?,?,?,?)",
                    (*key, source.revision, version, known_at),
                ).lastrowid
                db.executemany(
                    "INSERT INTO claims VALUES (?,?,?,?,?,?,?,?,?)",
                    [
                        (
                            generation,
                            i,
                            c.subject,
                            c.predicate,
                            c.value,
                            valid_from[i],
                            valid_to[i],
                            c.evidence_anchor,
                            c.evidence_kind,
                        )
                        for i, c in enumerate(claims)
                    ],
                )
                db.execute(
                    "UPDATE objects SET active_generation=?,source_head_revision=? "
                    "WHERE provider=? "
                    "AND account_id=? AND object_type=? AND external_id=?",
                    (generation, source.revision, *key),
                )
            if cursor is not None:
                advance_checkpoint(
                    db,
                    source.provider,
                    source.account_id,
                    cursor,
                    expected_cursor,
                )
            return changed

    async def checkpoint(self, provider: str, account_id: str) -> str | None:
        return await asyncio.to_thread(self._checkpoint, provider, account_id)

    def _checkpoint(self, provider: str, account_id: str) -> str | None:
        with self._connect() as db:
            return fetch_checkpoint(db, provider, account_id)
