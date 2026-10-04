"""Disabled-by-default durable delivery for one synthetic account.

Capture and its upstream cursor commit together. A leased delivery is applied
through the existing transaction-aware shadow ingest and acknowledged in that
same transaction. No source revision is derived from receipt or retry order.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from vera_shared.db.shadow_pg_ingest import PgShadowIngest
from vera_shared.ingest.shadow_types import Claim, Quarantine, SourceRevision

PROVIDER = "synthetic"
ACCOUNT = "queue-fixture"
SCOPE = f"{PROVIDER}:{ACCOUNT}"


def _enabled() -> None:
    if os.environ.get("VERA_SHADOW_SYNTHETIC_DELIVERY_ENABLED") != "1":
        raise Quarantine("synthetic delivery is disabled")


def _idle(conn: AsyncConnection) -> None:
    if conn.in_transaction():
        raise Quarantine("delivery requires idle connection")


@dataclass(frozen=True)
class Delivery:
    delivery_id: str
    source: SourceRevision
    claims: tuple[Claim, ...]
    known_at: str

    def payload(self) -> dict[str, object]:
        if (
            not self.delivery_id
            or self.source.provider != PROVIDER
            or self.source.account_id != ACCOUNT
            or self.source.required_scope != SCOPE
            or not self.source.revision
        ):
            raise Quarantine("untrusted synthetic delivery identity or revision")
        return {
            "source": asdict(self.source),
            "claims": [asdict(claim) for claim in self.claims],
            "known_at": self.known_at,
        }


async def capture(
    conn: AsyncConnection,
    *,
    expected_cursor: str | None,
    next_cursor: str,
    deliveries: list[Delivery],
) -> bool:
    """Persist a complete fetched page and its cursor; exact page replay is safe."""
    _enabled()
    _idle(conn)
    if not next_cursor or next_cursor == expected_cursor:
        raise Quarantine("capture cursor must advance")
    ids = [item.delivery_id for item in deliveries]
    if len(set(ids)) != len(ids):
        raise Quarantine("duplicate delivery ID in capture page")
    payloads = [(item.delivery_id, item.payload()) for item in deliveries]
    manifest = [{"delivery_id": did, "payload": payload} for did, payload in payloads]
    async with conn.begin():
        key = {"provider": PROVIDER, "account": ACCOUNT}
        await conn.execute(
            text(
                "INSERT INTO brain_delivery_cursors(provider,account_id) "
                "VALUES (:provider,:account) ON CONFLICT DO NOTHING"
            ),
            key,
        )
        current = (
            await conn.execute(
                text(
                    "SELECT cursor FROM brain_delivery_cursors WHERE provider=:provider "
                    "AND account_id=:account FOR UPDATE"
                ),
                key,
            )
        ).scalar_one()
        if current == next_cursor:
            stored = (
                await conn.execute(
                    text(
                        "SELECT expected_cursor,manifest FROM brain_delivery_batches "
                        "WHERE provider=:provider AND account_id=:account "
                        "AND next_cursor=:next_cursor"
                    ),
                    key | {"next_cursor": next_cursor},
                )
            ).one_or_none()
            if (
                stored is None
                or stored.expected_cursor != expected_cursor
                or stored.manifest != manifest
            ):
                raise Quarantine("capture page changed on replay")
            return False
        if current != expected_cursor:
            raise Quarantine("capture cursor changed concurrently")
        await conn.execute(
            text(
                "INSERT INTO brain_delivery_batches(provider,account_id,next_cursor,"
                "expected_cursor,manifest) VALUES (:provider,:account,:next_cursor,"
                ":expected_cursor,CAST(:manifest AS jsonb))"
            ),
            key
            | {
                "next_cursor": next_cursor,
                "expected_cursor": expected_cursor,
                "manifest": json.dumps(manifest, sort_keys=True),
            },
        )
        for did, payload in payloads:
            await conn.execute(
                text(
                    "INSERT INTO brain_delivery_envelopes(provider,account_id,delivery_id,"
                    "capture_cursor,payload) VALUES (:provider,:account,:delivery_id,"
                    ":next_cursor,CAST(:payload AS jsonb)) ON CONFLICT DO NOTHING"
                ),
                key
                | {
                    "delivery_id": did,
                    "next_cursor": next_cursor,
                    "payload": json.dumps(payload, sort_keys=True),
                },
            )
            stored = (
                await conn.execute(
                    text(
                        "SELECT capture_cursor,payload FROM brain_delivery_envelopes "
                        "WHERE provider=:provider AND account_id=:account "
                        "AND delivery_id=:delivery_id"
                    ),
                    key | {"delivery_id": did},
                )
            ).one()
            if stored.capture_cursor != next_cursor or stored.payload != payload:
                raise Quarantine("delivery ID reused with different payload")
        await conn.execute(
            text(
                "UPDATE brain_delivery_cursors SET cursor=:next_cursor "
                "WHERE provider=:provider AND account_id=:account"
            ),
            key | {"next_cursor": next_cursor},
        )
    return True


async def _claim(conn: AsyncConnection, lease_seconds: int) -> tuple[int, str] | None:
    token = uuid4().hex
    async with conn.begin():
        row = (
            await conn.execute(
                text(
                    "SELECT id,status,lease_until,next_attempt_at "
                    "FROM brain_delivery_envelopes "
                    "WHERE provider=:provider AND account_id=:account AND status<>'done' "
                    "ORDER BY id LIMIT 1 FOR UPDATE"
                ),
                {"provider": PROVIDER, "account": ACCOUNT},
            )
        ).one_or_none()
        if row is None or row.status == "quarantined":
            return None
        if row.status == "pending" and row.next_attempt_at is not None:
            ready = (
                await conn.execute(
                    text("SELECT now() >= :retry_at"),
                    {"retry_at": row.next_attempt_at},
                )
            ).scalar_one()
            if not ready:
                return None
        active = (
            (
                await conn.execute(text("SELECT now() < :until"), {"until": row.lease_until})
            ).scalar_one()
            if row.lease_until
            else False
        )
        if row.status == "leased" and active:
            return None
        await conn.execute(
            text(
                "UPDATE brain_delivery_envelopes SET status='leased',lease_token=:token,"
                "lease_until=now()+(:seconds * interval '1 second'),"
                "next_attempt_at=NULL,attempts=attempts+1 "
                "WHERE id=:id"
            ),
            {"id": row.id, "token": token, "seconds": lease_seconds},
        )
        return row.id, token


async def _settle_failure(conn: AsyncConnection, row_id: int, token: str, error: Exception) -> None:
    async with conn.begin():
        await conn.execute(
            text(
                "UPDATE brain_delivery_envelopes SET "
                "status=CASE WHEN :poison THEN 'quarantined' ELSE 'pending' END,"
                "lease_token=NULL,lease_until=NULL,"
                "next_attempt_at=CASE WHEN :poison THEN NULL ELSE "
                "now()+(LEAST(power(2,LEAST(attempts,10)),3600)*interval '1 second') END,"
                "error=:error "
                "WHERE id=:id AND status='leased' AND lease_token=:token"
            ),
            {
                "id": row_id,
                "token": token,
                "error": str(error)[:1000],
                "poison": isinstance(error, Quarantine),
            },
        )


async def deliver_once(conn: AsyncConnection, *, lease_seconds: int = 30) -> bool:
    """Apply the oldest envelope. Return False when none is currently available."""
    _enabled()
    _idle(conn)
    if lease_seconds < 1:
        raise ValueError("lease must be positive")
    claim = await _claim(conn, lease_seconds)
    if claim is None:
        return False
    row_id, token = claim
    try:
        async with conn.begin():
            row = (
                await conn.execute(
                    text(
                        "SELECT payload FROM brain_delivery_envelopes WHERE id=:id "
                        "AND status='leased' AND lease_token=:token FOR UPDATE"
                    ),
                    {"id": row_id, "token": token},
                )
            ).one_or_none()
            if row is None:
                return False
            data = row.payload
            source = SourceRevision(**data["source"])
            claims = [Claim(**value) for value in data["claims"]]
            if (
                source.provider != PROVIDER
                or source.account_id != ACCOUNT
                or source.required_scope != SCOPE
            ):
                raise Quarantine("stored synthetic identity changed")
            await PgShadowIngest({(PROVIDER, ACCOUNT): SCOPE}).apply(
                conn, source, claims, known_at=data["known_at"], in_transaction=True
            )
            await conn.execute(
                text(
                    "UPDATE brain_delivery_envelopes SET status='done',lease_token=NULL,"
                    "lease_until=NULL,completed_at=now(),error=NULL WHERE id=:id"
                ),
                {"id": row_id},
            )
    except Exception as exc:
        await _settle_failure(conn, row_id, token, exc)
        return True
    return True
