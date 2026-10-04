"""Account cursor lock and compare-and-swap in a caller-owned PG transaction."""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from vera_shared.ingest.shadow_types import Quarantine


async def _lock_checkpoint(conn: AsyncConnection, key: Mapping[str, str]) -> str | None:
    await conn.execute(
        text(
            "INSERT INTO brain_checkpoints(provider,account_id,cursor) "
            "VALUES (:provider,:account_id,NULL) ON CONFLICT DO NOTHING"
        ),
        key,
    )
    return (
        await conn.execute(
            text(
                "SELECT cursor FROM brain_checkpoints WHERE provider=:provider "
                "AND account_id=:account_id FOR UPDATE"
            ),
            key,
        )
    ).scalar_one()


async def _advance_checkpoint(
    conn: AsyncConnection,
    key: Mapping[str, str],
    *,
    current: str | None,
    cursor: str | None,
    expected_cursor: str | None,
) -> None:
    if cursor is None:
        return
    if current != cursor and current != expected_cursor:
        raise Quarantine("checkpoint changed concurrently")
    await conn.execute(
        text(
            "UPDATE brain_checkpoints SET cursor=:cursor WHERE provider=:provider "
            "AND account_id=:account_id"
        ),
        dict(key) | {"cursor": cursor},
    )
