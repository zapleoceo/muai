"""Общий сброс схемы для интеграционных тестов на живом Postgres."""
from __future__ import annotations

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

# Таблицы вне ORM-метаданных, которые ссылаются на ORM-таблицы. drop_all о них
# не знает и падает на внешнем ключе (13.09.2026: куски 032 оставались от
# test_postgres_sql и валили все тесты test_worker_race).
_RAW_DEPENDENT_TABLES = ("event_chunk_embeddings",)


async def reset_schema(conn: AsyncConnection) -> None:
    from vera_shared.db import (  # noqa: F401
        models,
        models_graph,
        models_links,
        models_mcp,
        models_pair_roles,
        models_room,
        models_sources,
    )
    from vera_shared.db.engine import Base

    for table in _RAW_DEPENDENT_TABLES:
        await conn.execute(sa_text(f"DROP TABLE IF EXISTS {table}"))
    await conn.run_sync(Base.metadata.drop_all)
    await conn.run_sync(Base.metadata.create_all)
    await _add_vector_column(conn)


async def _add_vector_column(conn: AsyncConnection) -> None:
    """event_embeddings.embedding_vec — вне ORM (у SQLite нет halfvec). Размерность
    3 вместо боевой 1024; расширения нет в сборке — таблица остаётся без колонки,
    а тесты, которым она нужна, пропускаются."""
    try:
        async with conn.begin_nested():
            await conn.execute(sa_text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.execute(sa_text(
                "ALTER TABLE event_embeddings ADD COLUMN embedding_vec halfvec(3) NOT NULL"))
    except DBAPIError:
        return


@pytest.fixture
def pg_schema_reset():
    return reset_schema
