from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

settings = get_settings()

engine = create_async_engine(
    settings.db_url_asyncpg,
    echo=False,
    pool_size=10,
    max_overflow=20,
)


@event.listens_for(engine.sync_engine, "connect")
def _on_connect(dbapi_conn, _):
    dbapi_conn.run_async(lambda conn: conn.set_type_codec(
        "vector",
        encoder=lambda v: str(v),
        decoder=lambda v: [float(x) for x in v.strip("[]").split(",")],
        schema="pg_catalog",
        format="text",
    ))


AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_session() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session
