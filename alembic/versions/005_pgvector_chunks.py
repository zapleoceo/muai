"""pgvector extension and message_chunks table"""
revision = "005"
down_revision = "004"


def upgrade() -> None:
    # SQL applied manually via psql — see CLAUDE.md
    pass


SQL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS message_chunks (
    id BIGSERIAL PRIMARY KEY,
    chat_id BIGINT NOT NULL REFERENCES chats(id),
    chat_title TEXT,
    chunk_text TEXT NOT NULL,
    embedding vector(512),
    msg_date_from TIMESTAMPTZ,
    msg_date_to TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_chunks_chat ON message_chunks (chat_id);
CREATE INDEX IF NOT EXISTS idx_chunks_date ON message_chunks (msg_date_from);
CREATE INDEX IF NOT EXISTS idx_chunks_embedding
    ON message_chunks USING hnsw (embedding vector_cosine_ops);
"""
