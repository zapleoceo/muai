"""Audited PostgreSQL restoration of an earlier shadow generation."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from vera_shared.ingest.shadow_types import Quarantine, utc_timestamp


class PgShadowRollback:
    def __init__(self, account_scopes: Mapping[tuple[str, str], str]):
        self.account_scopes = dict(account_scopes)

    async def restore(
        self,
        conn: AsyncConnection,
        key: tuple[str, str, str, str],
        *,
        target_generation: int,
        expected_active: int,
        known_at: str,
        reason: str,
        authorized_write_scopes: set[str],
    ) -> int:
        if conn.in_transaction():
            raise Quarantine("rollback requires idle connection")
        scope = self.account_scopes.get(key[:2])
        if scope is None or f"write:{scope}" not in authorized_write_scopes:
            raise Quarantine("source write access denied")
        if not reason.strip():
            raise Quarantine("rollback reason required")
        known = datetime.fromisoformat(utc_timestamp(known_at))
        params = dict(
            zip(("provider", "account_id", "object_type", "external_id"), key, strict=False)
        )
        async with conn.begin():
            active_id = (
                await conn.execute(
                    text(
                        "SELECT active_generation FROM brain_source_objects "
                        "WHERE provider=:provider AND account_id=:account_id "
                        "AND object_type=:object_type AND external_id=:external_id FOR UPDATE"
                    ),
                    params,
                )
            ).scalar_one_or_none()
            if active_id is None or active_id != expected_active or target_generation == active_id:
                raise Quarantine("rollback active generation changed")
            active = (
                await conn.execute(
                    text("SELECT known_from FROM brain_generations WHERE id=:id"),
                    {"id": active_id},
                )
            ).scalar_one()
            target = (
                await conn.execute(
                    text(
                        "SELECT revision,known_from FROM brain_generations WHERE id=:id "
                        "AND provider=:provider AND account_id=:account_id "
                        "AND object_type=:object_type AND external_id=:external_id"
                    ),
                    params | {"id": target_generation},
                )
            ).first()
            if target is None or known <= active or known <= target.known_from:
                raise Quarantine("invalid rollback target or knowledge time")
            await conn.execute(
                text("UPDATE brain_generations SET known_to=:known WHERE id=:id"),
                {"known": known, "id": active_id},
            )
            restored = (
                await conn.execute(
                    text(
                        "INSERT INTO brain_generations(provider,account_id,object_type,external_id,"
                        "revision,extraction_version,known_from) VALUES "
                        "(:provider,:account_id,:object_type,:external_id,:revision,:version,:known) "
                        "RETURNING id"
                    ),
                    params
                    | {
                        "revision": target.revision,
                        "version": f"restore:{target_generation}:{known.isoformat()}",
                        "known": known,
                    },
                )
            ).scalar_one()
            await conn.execute(
                text(
                    "INSERT INTO brain_claims(generation_id,ordinal,subject,predicate,value,"
                    "valid_from,valid_to,evidence_anchor,evidence_kind) "
                    "SELECT :restored,ordinal,subject,predicate,value,valid_from,valid_to,"
                    "evidence_anchor,evidence_kind FROM brain_claims WHERE generation_id=:target"
                ),
                {"restored": restored, "target": target_generation},
            )
            await conn.execute(
                text(
                    "UPDATE brain_source_objects SET active_generation=:restored "
                    "WHERE provider=:provider AND account_id=:account_id "
                    "AND object_type=:object_type AND external_id=:external_id"
                ),
                params | {"restored": restored},
            )
            await conn.execute(
                text(
                    "INSERT INTO brain_rollback_audit(provider,account_id,object_type,external_id,"
                    "from_generation,target_generation,restored_generation,known_at,reason) "
                    "VALUES (:provider,:account_id,:object_type,:external_id,:from_generation,"
                    ":target_generation,:restored_generation,:known_at,:reason)"
                ),
                params
                | {
                    "from_generation": active_id,
                    "target_generation": target_generation,
                    "restored_generation": restored,
                    "known_at": known,
                    "reason": reason,
                },
            )
            return restored
