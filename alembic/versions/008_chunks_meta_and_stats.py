"""message_chunks meta + stats

Revision ID: 008
Revises: 007
Create Date: 2026-05-13
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("message_chunks", sa.Column("min_msg_id", sa.BigInteger(), nullable=True))
    op.add_column("message_chunks", sa.Column("msg_count", sa.Integer(), nullable=True))
    op.add_column("message_chunks", sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=True))

    op.execute("CREATE INDEX IF NOT EXISTS idx_chunks_min_msg_id ON message_chunks (min_msg_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_chunks_max_msg_id ON message_chunks (max_msg_id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_chunks_max_msg_id")
    op.execute("DROP INDEX IF EXISTS idx_chunks_min_msg_id")
    op.drop_column("message_chunks", "meta")
    op.drop_column("message_chunks", "msg_count")
    op.drop_column("message_chunks", "min_msg_id")
