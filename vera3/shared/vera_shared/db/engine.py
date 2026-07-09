"""Async SQLAlchemy engine + session factory."""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

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


def _pool_kwargs() -> dict:
    """Postgres connection-pool sizing from env — split out of init_engine so
    it's unit-testable without a real Postgres connection (the branch that
    calls this is SQLite-excluded, and CI's test suite runs on aiosqlite).

    pool_size = сколько соединений держим ОТКРЫТЫМИ постоянно (в простое).
    max_overflow = временные сверх пула, закрываются после использования.
    Раньше pool_size=10 на КАЖДУЮ из ~13 служб → ~57 idle-коннектов забивали
    память хоста (swap). 3 хватает низконагруженным сервисам; overflow=7
    сохраняет burst до 10 при пиках (эти соединения не висят в простое).
    DB_POOL_SIZE позволяет поднять на службе, которой реально нужно больше.
    """
    return {
        "pool_pre_ping": True,
        "pool_size": int(os.environ.get("DB_POOL_SIZE", "3")),
        "max_overflow": int(os.environ.get("DB_MAX_OVERFLOW", "7")),
        "pool_recycle": 1800,   # переоткрывать соединение раз в 30 мин (свежесть)
        "pool_timeout": 30,
    }


async def init_engine(url: str | None = None, echo: bool = False) -> AsyncEngine:
    """Lazy инициализация engine. Idempotent."""
    global _engine, AsyncSessionLocal
    if _engine is not None:
        return _engine
    actual_url = url or database_url()
    kwargs: dict = {"echo": echo}
    # Pool sizing applies only to real databases, не SQLite.
    if not actual_url.startswith("sqlite"):
        kwargs.update(_pool_kwargs())
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
