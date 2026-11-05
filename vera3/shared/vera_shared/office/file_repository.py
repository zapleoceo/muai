"""Storage quota and expiry SQL; preserve legacy rows without an expiry."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.models_office import OfficeImageRow
from vera_shared.timeutil import utc_naive_now

RETENTION = timedelta(hours=24)
MAX_STORED_BYTES = 64 * 1024 * 1024


class StorageFull(RuntimeError):
    """Retryable capacity exhaustion."""


async def lock_storage(s: AsyncSession) -> None:
    if s.bind is not None and s.bind.dialect.name == "postgresql":
        await s.execute(text("SELECT pg_advisory_xact_lock(683168109372)"))


async def require_capacity(s: AsyncSession, size: int) -> None:
    await lock_storage(s)
    used = (await s.execute(select(func.coalesce(func.sum(func.length(
        OfficeImageRow.content)), 0)).where(OfficeImageRow.expires_at.is_not(None)))).scalar_one()
    if used + size > MAX_STORED_BYTES:
        raise StorageFull("Office temporary storage is full; retry after expiry cleanup")


async def prune_expired(s: AsyncSession) -> int:
    await lock_storage(s)
    result = await s.execute(delete(OfficeImageRow).where(
        OfficeImageRow.expires_at.is_not(None),
        OfficeImageRow.expires_at <= utc_naive_now()))
    return result.rowcount
