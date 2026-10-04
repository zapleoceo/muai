"""Advisory-замок построителя связей: общий для цикла brain-triage и ручных пересчётов."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text

from vera_shared.db.engine import get_engine

__all__ = ["LINKS_LOCK_KEY", "LinksBusyError", "links_lock", "require_links_lock"]

LINKS_LOCK_KEY = 7_340_042


class LinksBusyError(RuntimeError):
    """Замок построителя связей занят: идёт проход цикла или другой пересчёт."""


@asynccontextmanager
async def links_lock() -> AsyncIterator[bool]:
    """Замок уровня сессии на отдельном соединении в AUTOCOMMIT: открытой транзакции на время
    прохода нет (idle-in-transaction держал бы снимок и мешал вакууму). На не-Postgres
    (тесты) замка нет — проход свободен."""
    engine = get_engine()
    if engine.dialect.name != "postgresql":
        yield True
        return
    async with engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        got = bool((await conn.execute(text("SELECT pg_try_advisory_lock(:k)"),
                                       {"k": LINKS_LOCK_KEY})).scalar_one())
        try:
            yield got
        finally:
            if got:
                await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LINKS_LOCK_KEY})


@asynccontextmanager
async def require_links_lock() -> AsyncIterator[None]:
    """Как `links_lock`, но занятый замок — `LinksBusyError`: пересчёт не должен гоняться с циклом
    за одни и те же строки `event_entities`; повторить можно через минуту."""
    async with links_lock() as got:
        if not got:
            raise LinksBusyError("построитель связей занят (проход цикла или другой пересчёт) — "
                                 "повторите позже")
        yield
