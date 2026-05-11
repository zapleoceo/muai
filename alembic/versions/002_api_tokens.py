"""api_tokens table

Revision ID: 002
Revises: 001
Create Date: 2026-05-11
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "api_tokens",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("provider", sa.Text(), nullable=False, server_default="gemini"),
        sa.Column("token", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=True, server_default="true"),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("last_used_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("error_count", sa.BigInteger(), nullable=True, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_api_tokens_provider", "api_tokens", ["provider", "is_active"])


def downgrade() -> None:
    op.drop_index("idx_api_tokens_provider")
    op.drop_table("api_tokens")
