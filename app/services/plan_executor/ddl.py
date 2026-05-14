from sqlalchemy import text

from app.db.database import AsyncSessionLocal


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
    ddl = [
        "CREATE EXTENSION IF NOT EXISTS vector",
        "ALTER TABLE message_chunks ADD COLUMN IF NOT EXISTS min_msg_id bigint",
        "ALTER TABLE message_chunks ADD COLUMN IF NOT EXISTS msg_count integer",
        "ALTER TABLE message_chunks ADD COLUMN IF NOT EXISTS meta jsonb",
        "CREATE INDEX IF NOT EXISTS idx_chunks_min_msg_id ON message_chunks (min_msg_id)",
        "CREATE INDEX IF NOT EXISTS idx_chunks_max_msg_id ON message_chunks (max_msg_id)",
        "CREATE INDEX IF NOT EXISTS idx_chunks_embedding_hnsw ON message_chunks USING hnsw (embedding vector_cosine_ops)",
        """
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
            embedding vector(768),
            meta jsonb,
            created_at TIMESTAMPTZ DEFAULT now(),
            CONSTRAINT uq_media_chunks_chat_tg_msg UNIQUE (chat_id, source_tg_msg_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_media_chunks_chat ON media_chunks (chat_id)",
        "CREATE INDEX IF NOT EXISTS idx_media_chunks_date ON media_chunks (date_utc)",
        "CREATE INDEX IF NOT EXISTS idx_media_chunks_embedding_hnsw ON media_chunks USING hnsw (embedding vector_cosine_ops)",
    ]
    async with AsyncSessionLocal() as session:
        for stmt in ddl:
            await session.execute(text(stmt))
        await session.commit()
