"""media_chunks table

Revision ID: 009
Revises: 008
Create Date: 2026-05-14
"""

revision = "009"
down_revision = "008"


def upgrade() -> None:
    pass


SQL = """
CREATE EXTENSION IF NOT EXISTS vector;

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
);

CREATE INDEX IF NOT EXISTS idx_media_chunks_chat ON media_chunks (chat_id);
CREATE INDEX IF NOT EXISTS idx_media_chunks_date ON media_chunks (date_utc);
"""

