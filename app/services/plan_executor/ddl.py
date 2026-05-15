import logging

from sqlalchemy import text

from app.db.database import AsyncSessionLocal

logger = logging.getLogger(__name__)

_EMBEDDING_DIMS = 512


async def _vector_dim(session, *, table: str, column: str) -> int | None:
    type_str = (await session.execute(
        text(
            """
            SELECT format_type(a.atttypid, a.atttypmod) AS type_str
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = current_schema()
              AND c.relname = :table
              AND a.attname = :col
              AND a.attnum > 0
              AND NOT a.attisdropped
            LIMIT 1
            """
        ),
        {"table": table, "col": column},
    )).scalar_one_or_none()
    if not type_str:
        return None
    s = str(type_str)
    if s.startswith("vector(") and s.endswith(")"):
        inside = s[len("vector("):-1]
        if inside.isdigit():
            return int(inside)
    return None


async def _ensure_vector_dim(session, *, table: str, column: str, dims: int) -> None:
    dim = await _vector_dim(session, table=table, column=column)
    if dim == int(dims):
        return
    logger.warning(
        "Vector dim mismatch in %s.%s (current=%s, required=%d) — "
        "dropping column and truncating table; all existing embeddings will be lost",
        table, column, dim, dims,
    )
    if table == "message_chunks":
        await session.execute(text("DROP INDEX IF EXISTS idx_chunks_embedding_hnsw"))
        await session.execute(text("DROP INDEX IF EXISTS idx_chunks_embedding"))
    if table == "media_chunks":
        await session.execute(text("DROP INDEX IF EXISTS idx_media_chunks_embedding_hnsw"))
    await session.execute(text(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}"))
    await session.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} vector({int(dims)})"))
    await session.execute(text(f"TRUNCATE TABLE {table}"))


async def ensure_search_infra() -> None:
    ddl = [
        "CREATE EXTENSION IF NOT EXISTS pg_trgm",
        "CREATE INDEX IF NOT EXISTS idx_messages_fts_simple ON messages USING GIN (to_tsvector('simple', coalesce(text,'') || ' ' || coalesce(caption,'')))",
        "CREATE INDEX IF NOT EXISTS idx_messages_fts_ru ON messages USING GIN (to_tsvector('russian', coalesce(text,'') || ' ' || coalesce(caption,'')))",
        "CREATE INDEX IF NOT EXISTS idx_messages_fts_en ON messages USING GIN (to_tsvector('english', coalesce(text,'') || ' ' || coalesce(caption,'')))",
        "CREATE INDEX IF NOT EXISTS idx_messages_trgm ON messages USING GIN ((coalesce(text,'') || ' ' || coalesce(caption,'')) gin_trgm_ops)",
        "CREATE INDEX IF NOT EXISTS idx_chats_title_trgm ON chats USING GIN (coalesce(title,'') gin_trgm_ops)",
        "CREATE INDEX IF NOT EXISTS idx_chats_username_trgm ON chats USING GIN (coalesce(username,'') gin_trgm_ops)",
        "CREATE INDEX IF NOT EXISTS idx_chats_folder_trgm ON chats USING GIN (coalesce(folder,'') gin_trgm_ops)",
    ]
    async with AsyncSessionLocal() as session:
        for stmt in ddl:
            await session.execute(text(stmt))
        await session.commit()


async def ensure_chunk_schema() -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))

        await session.execute(text("ALTER TABLE message_chunks ADD COLUMN IF NOT EXISTS min_msg_id bigint"))
        await session.execute(text("ALTER TABLE message_chunks ADD COLUMN IF NOT EXISTS msg_count integer"))
        await session.execute(text("ALTER TABLE message_chunks ADD COLUMN IF NOT EXISTS meta jsonb"))

        await session.execute(text(f"""
            CREATE TABLE IF NOT EXISTS media_chunks (
                id BIGSERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL REFERENCES chats(id),
                chat_title TEXT,
                chat_username TEXT,
                source_msg_id BIGINT NOT NULL,
                source_tg_msg_id BIGINT,
                media_type TEXT NOT NULL,
                date_utc TIMESTAMPTZ,
                chunk_text TEXT NOT NULL,
                embedding vector({_EMBEDDING_DIMS}),
                meta jsonb,
                created_at TIMESTAMPTZ DEFAULT now(),
                CONSTRAINT uq_media_chunks_chat_tg_msg UNIQUE (chat_id, source_tg_msg_id)
            )
        """))

        await _ensure_vector_dim(session, table="message_chunks", column="embedding", dims=_EMBEDDING_DIMS)
        await _ensure_vector_dim(session, table="media_chunks", column="embedding", dims=_EMBEDDING_DIMS)

        await session.execute(text("CREATE INDEX IF NOT EXISTS idx_chunks_min_msg_id ON message_chunks (min_msg_id)"))
        await session.execute(text("CREATE INDEX IF NOT EXISTS idx_chunks_max_msg_id ON message_chunks (max_msg_id)"))
        await session.execute(text("CREATE INDEX IF NOT EXISTS idx_chunks_embedding_hnsw ON message_chunks USING hnsw (embedding vector_cosine_ops)"))
        await session.execute(text("CREATE INDEX IF NOT EXISTS idx_media_chunks_chat ON media_chunks (chat_id)"))
        await session.execute(text("CREATE INDEX IF NOT EXISTS idx_media_chunks_date ON media_chunks (date_utc)"))
        await session.execute(text("CREATE INDEX IF NOT EXISTS idx_media_chunks_embedding_hnsw ON media_chunks USING hnsw (embedding vector_cosine_ops)"))
        await session.commit()
