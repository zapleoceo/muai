"""Runtime control flags — a tiny key/value table workers poll each loop.

Currently used for the backfill pause switch: the dashboard sets
`backfill_paused=1`, and brain-triage + media-worker skip claiming work
until it's cleared. Survives restarts (it's in Postgres), so a pause
holds across deploys.
"""
from __future__ import annotations

from sqlalchemy import text

from vera_shared.db.engine import get_session

BACKFILL_PAUSED = "backfill_paused"


async def get_control(key: str, default: str = "") -> str:
    async with get_session() as s:
        row = (await s.execute(
            text("SELECT value FROM app_control WHERE key = :k"), {"k": key}
        )).scalar_one_or_none()
    return row if row is not None else default


async def set_control(key: str, value: str) -> None:
    async with get_session() as s:
        await s.execute(text("""
            INSERT INTO app_control (key, value, updated_at)
            VALUES (:k, :v, now())
            ON CONFLICT (key) DO UPDATE SET value = :v, updated_at = now()
        """), {"k": key, "v": value})


async def is_backfill_paused() -> bool:
    return (await get_control(BACKFILL_PAUSED, "0")) == "1"


async def set_backfill_paused(paused: bool) -> None:
    await set_control(BACKFILL_PAUSED, "1" if paused else "0")
