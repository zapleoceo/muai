"""Async SQLAlchemy engine + session factory."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base for all ORM models."""


_engine: AsyncEngine | None = None
AsyncSessionLocal: async_sessionmaker[AsyncSession] | None = None


def database_url() -> str:
    """Postgres URL из env. Дефолт для local-dev."""
    return os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://vera:vera@localhost:5432/vera",
    )


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("Engine not initialized. Call init_engine() first.")
    return _engine


async def init_engine(url: str | None = None, echo: bool = False) -> AsyncEngine:
    """Lazy инициализация engine. Idempotent."""
    global _engine, AsyncSessionLocal
    if _engine is not None:
        return _engine
    actual_url = url or database_url()
    kwargs: dict = {"echo": echo}
    # Pool sizing applies only to real databases, не SQLite
    if not actual_url.startswith("sqlite"):
        kwargs.update(pool_pre_ping=True, pool_size=10, max_overflow=20)
    _engine = create_async_engine(actual_url, **kwargs)
    AsyncSessionLocal = async_sessionmaker(
        bind=_engine, expire_on_commit=False, class_=AsyncSession,
    )
    return _engine


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """Async session context manager. Auto-commit on success, rollback on error."""
    if AsyncSessionLocal is None:
        await init_engine()
    assert AsyncSessionLocal is not None
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def close_engine() -> None:
    """Shutdown — закрыть pool."""
    global _engine, AsyncSessionLocal
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        AsyncSessionLocal = None
