"""Trusted account gate, legacy ID link, and derived cache invalidation."""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from vera_shared.ingest.shadow_types import Quarantine, SourceRevision


async def _require_active_instagram(
    conn: AsyncConnection, source: SourceRevision, account: str | None
) -> None:
    if account is None:
        return
    permitted = (
        await conn.execute(
            text(
                "SELECT 1 FROM instagram_sessions WHERE username=:account AND is_active FOR SHARE"
            ),
            {"account": account},
        )
    ).scalar_one_or_none()
    if source.provider != "instagram" or source.account_id != account or not permitted:
        raise Quarantine("Instagram account access revoked")


async def _link_legacy_event(
    conn: AsyncConnection, key: Mapping[str, str], event_id: int | None
) -> None:
    if event_id is None:
        return
    await conn.execute(
        text(
            "INSERT INTO brain_event_links(event_id,provider,account_id,object_type,external_id) "
            "VALUES (:event_id,:provider,:account_id,:object_type,:external_id) "
            "ON CONFLICT DO NOTHING"
        ),
        dict(key) | {"event_id": event_id},
    )
    linked = (
        await conn.execute(
            text(
                "SELECT event_id FROM brain_event_links WHERE provider=:provider "
                "AND account_id=:account_id AND object_type=:object_type "
                "AND external_id=:external_id"
            ),
            key,
        )
    ).scalar_one_or_none()
    if linked != event_id:
        raise Quarantine("legacy event identity changed")


async def _invalidate_source_cache(conn: AsyncConnection, key: Mapping[str, str]) -> None:
    await conn.execute(
        text("DELETE FROM brain_derived_cache WHERE provider=:provider AND account_id=:account_id"),
        key,
    )
