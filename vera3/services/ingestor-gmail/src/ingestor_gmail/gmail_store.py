"""SQL поллера Gmail: какие письма уже забраны, какие аккаунты опрашивать,
и статус аккаунта после прогона."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from vera_shared.db.engine import get_session
from vera_shared.db.models import EventRow
from vera_shared.db.models_sources import GmailAccountRow


async def load_known_message_ids(ids: list[str]) -> set[str]:
    async with get_session() as s:
        return set((await s.execute(
            select(EventRow.source_event_id).where(
                EventRow.source == "gmail",
                EventRow.source_event_id.in_(ids),
            )
        )).scalars().all())


async def load_pollable_accounts() -> list[GmailAccountRow]:
    # Пропускаем needs_reauth — токен мёртв, refresh бесполезен,
    # вернётся в опрос после переподключения через дашборд.
    async with get_session() as s:
        return list((await s.execute(
            select(GmailAccountRow)
            .where(GmailAccountRow.is_active.is_(True))
            .where(GmailAccountRow.needs_reauth.is_(False))
        )).scalars().all())


async def mark_account_failed(account_id: int, error: str, *, needs_reauth: bool) -> None:
    """Записать ошибку прогона; needs_reauth=True выводит аккаунт из опроса."""
    values: dict[str, Any] = {"last_error": error}
    if needs_reauth:
        values["needs_reauth"] = True
    async with get_session() as s:
        await s.execute(
            update(GmailAccountRow).where(GmailAccountRow.id == account_id).values(**values)
        )


async def mark_account_polled(account_id: int, now: datetime, *, advance_cursor: bool) -> None:
    values: dict[str, Any] = {"last_ok_at": now, "needs_reauth": False, "last_error": None}
    if advance_cursor:
        values["last_polled_at"] = now
    async with get_session() as s:
        await s.execute(
            update(GmailAccountRow).where(GmailAccountRow.id == account_id).values(**values)
        )
