from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy import select, text

from vera_shared.db.models import Base, Token
from vera_shared.db.engine import get_session

_DEFAULT_CAPS: dict[str, list[str]] = {
    "gemini": ["chat:fast", "prefilter"],
    "deepseek": ["chat:smart", "chat:code"],
    "voyage": ["embed"],
    "anthropic": ["chat:smart", "chat:code"],
}


async def run_migrations(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # WAL mode for better concurrent read/write
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.execute(text("PRAGMA synchronous=NORMAL"))

    await _seed_default_caps()


async def _seed_default_caps() -> None:
    async with get_session() as session:
        result = await session.execute(select(Token).limit(1))
        if result.scalar_one_or_none() is not None:
            return

        # No tokens yet — nothing to seed. Actual token values come from env/UI.
        # We only store capability metadata, not keys, so no seeding needed here.
