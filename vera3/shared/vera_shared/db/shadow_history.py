"""Scope-bound reads and audited restoration for the offline shadow model."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from vera_shared.db.shadow_control import restore_generation
from vera_shared.db.shadow_queries import fetch_claims, fetch_generation_ids
from vera_shared.ingest.shadow_types import Quarantine, utc_timestamp


class ShadowHistory:
    def __init__(self, path: str | Path, account_scopes: Mapping[tuple[str, str], str]):
        self.path = str(path)
        self.account_scopes = dict(account_scopes)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path)
        db.execute("PRAGMA foreign_keys = ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    async def current_claims(self, authorized_scopes: set[str]) -> list[dict[str, str]]:
        return await asyncio.to_thread(self._current_claims, authorized_scopes)

    def _current_claims(self, authorized_scopes: set[str]) -> list[dict[str, str]]:
        with self._connect() as db:
            return fetch_claims(db, self.account_scopes, authorized_scopes)

    async def claims_as_of(
        self, authorized_scopes: set[str], known_at: str
    ) -> list[dict[str, str]]:
        return await asyncio.to_thread(self._claims_as_of, authorized_scopes, known_at)

    def _claims_as_of(self, authorized_scopes: set[str], known_at: str) -> list[dict[str, str]]:
        with self._connect() as db:
            return fetch_claims(
                db, self.account_scopes, authorized_scopes, as_of=utc_timestamp(known_at)
            )

    def _require_scope(self, key: tuple[str, str, str, str], authorized_scopes: set[str]) -> None:
        scope = self.account_scopes.get(key[:2])
        if scope is None or scope not in authorized_scopes:
            raise Quarantine("source access denied")

    async def generation_ids(
        self, key: tuple[str, str, str, str], authorized_scopes: set[str]
    ) -> list[int]:
        self._require_scope(key, authorized_scopes)
        return await asyncio.to_thread(self._generation_ids, key)

    def _generation_ids(self, key: tuple[str, str, str, str]) -> list[int]:
        with self._connect() as db:
            return fetch_generation_ids(db, key)

    async def rollback(
        self,
        key: tuple[str, str, str, str],
        *,
        target_generation: int,
        expected_active: int,
        known_at: str,
        reason: str,
        authorized_write_scopes: set[str],
    ) -> int:
        scope = self.account_scopes.get(key[:2])
        if scope is None or f"write:{scope}" not in authorized_write_scopes:
            raise Quarantine("source write access denied")
        return await asyncio.to_thread(
            self._rollback, key, target_generation, expected_active, utc_timestamp(known_at), reason
        )

    def _rollback(
        self, key: tuple[str, str, str, str], target: int, expected: int, known_at: str, reason: str
    ) -> int:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return restore_generation(
                db,
                key,
                target_generation=target,
                expected_active=expected,
                known_at=known_at,
                reason=reason,
            )
