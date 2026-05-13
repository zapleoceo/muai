"""search indexes (fts + trgm)

Revision ID: 007
Revises: 006
Create Date: 2026-05-13
"""
from typing import Sequence, Union

from alembic import op

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_messages_fts_simple
        ON messages
        USING GIN (to_tsvector('simple', coalesce(text,'') || ' ' || coalesce(caption,'')))
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_messages_fts_ru
        ON messages
        USING GIN (to_tsvector('russian', coalesce(text,'') || ' ' || coalesce(caption,'')))
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_messages_fts_en
        ON messages
        USING GIN (to_tsvector('english', coalesce(text,'') || ' ' || coalesce(caption,'')))
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_messages_trgm
        ON messages
        USING GIN ((coalesce(text,'') || ' ' || coalesce(caption,'')) gin_trgm_ops)
        """
    )

    op.execute("CREATE INDEX IF NOT EXISTS idx_chats_title_trgm ON chats USING GIN (coalesce(title,'') gin_trgm_ops)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_chats_username_trgm ON chats USING GIN (coalesce(username,'') gin_trgm_ops)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_chats_folder_trgm ON chats USING GIN (coalesce(folder,'') gin_trgm_ops)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_chats_folder_trgm")
    op.execute("DROP INDEX IF EXISTS idx_chats_username_trgm")
    op.execute("DROP INDEX IF EXISTS idx_chats_title_trgm")
    op.execute("DROP INDEX IF EXISTS idx_messages_trgm")
    op.execute("DROP INDEX IF EXISTS idx_messages_fts_en")
    op.execute("DROP INDEX IF EXISTS idx_messages_fts_ru")
    op.execute("DROP INDEX IF EXISTS idx_messages_fts_simple")
