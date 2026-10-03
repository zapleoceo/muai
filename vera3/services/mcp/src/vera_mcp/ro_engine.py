"""Отдельное подключение для `sql_query` — под ролью `vera_ro`, не под `vera`.

Основная роль `vera` — суперпользователь: разбор текста запроса её не
удержит (`SELECT query_to_xml('select pg_read_file(...)', ...)` проходит
любой список запретов, потому что опасный SQL лежит в строковом литерале).
Поэтому `sql_query` ходит в БД ролью без суперправ и с SELECT только на
таблицы с содержимым мозга (миграция 037_mcp_ro_role), а сервис отказывается
работать, если URL не задан или роль оказалась суперпользователем.
"""
from __future__ import annotations

import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

ENV_NAME = "MCP_RO_DATABASE_URL"


class ReadOnlyUnavailable(RuntimeError):
    """sql_query отключён: нет безопасного подключения."""


_cached: tuple[str, AsyncEngine] | None = None


async def _assert_not_superuser(engine: AsyncEngine) -> None:
    if engine.dialect.name != "postgresql":
        return
    async with engine.connect() as conn:
        is_super = (await conn.execute(text(
            "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
        ))).scalar_one()
    if is_super:
        raise ReadOnlyUnavailable(
            f"{ENV_NAME} connects as a superuser; sql_query is disabled. "
            "Use the restricted role vera_ro (see docs/mcp-claude.md).")


async def get_ro_engine() -> AsyncEngine:
    """Движок под `MCP_RO_DATABASE_URL`; роль проверяется один раз на URL."""
    global _cached
    url = os.environ.get(ENV_NAME, "").strip()
    if not url:
        raise ReadOnlyUnavailable(
            f"{ENV_NAME} is not set; sql_query is disabled until the restricted "
            "role vera_ro is configured (see docs/mcp-claude.md).")
    if _cached is not None and _cached[0] == url:
        return _cached[1]
    kwargs = {} if url.startswith("sqlite") else {
        "pool_size": 2, "max_overflow": 2, "pool_pre_ping": True, "pool_recycle": 1800}
    engine = create_async_engine(url, **kwargs)
    try:
        await _assert_not_superuser(engine)
    except BaseException:
        await engine.dispose()
        raise
    await forget_ro_engine()
    _cached = (url, engine)
    return engine


async def forget_ro_engine() -> None:
    global _cached
    if _cached is not None:
        old, _cached = _cached[1], None
        await old.dispose()
