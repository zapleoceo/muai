"""Общий сброс схемы для интеграционных тестов на живом Postgres."""
from __future__ import annotations

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncConnection

# Таблицы вне ORM-метаданных, которые ссылаются на ORM-таблицы. drop_all о них
# не знает и падает на внешнем ключе (13.09.2026: куски 032 оставались от
# test_postgres_sql и валили все тесты test_worker_race).
_RAW_DEPENDENT_TABLES = ("event_chunk_embeddings",)


async def reset_schema(conn: AsyncConnection) -> None:
    from vera_shared.db import models, models_graph, models_sources  # noqa: F401
    from vera_shared.db.engine import Base

    for table in _RAW_DEPENDENT_TABLES:
        await conn.execute(sa_text(f"DROP TABLE IF EXISTS {table}"))
    await conn.run_sync(Base.metadata.drop_all)
    await conn.run_sync(Base.metadata.create_all)


@pytest.fixture
def pg_schema_reset():
    return reset_schema
