import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker | None = None


def get_engine() -> AsyncEngine:
    global _engine, _session_factory
    if _engine is None:
        db_path = os.getenv("DB_PATH", "/data/vera.db")
        url = f"sqlite+aiosqlite:///{db_path}"
        _engine = create_async_engine(
            url,
            connect_args={"check_same_thread": False},
            echo=False,
        )
        _session_factory = async_sessionmaker(
            _engine, expire_on_commit=False, class_=AsyncSession
        )
    return _engine


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    if _session_factory is None:
        get_engine()
    async with _session_factory() as session:  # type: ignore[misc]
        yield session


async def _enable_wal(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        await conn.exec_driver_sql("PRAGMA synchronous=NORMAL")


async def init_engine() -> AsyncEngine:
    engine = get_engine()
    await _enable_wal(engine)
    return engine
