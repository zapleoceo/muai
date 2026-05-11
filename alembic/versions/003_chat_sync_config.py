"""chat_sync_config table

Revision ID: 003
Revises: 002
Create Date: 2025-05-11
"""
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_sync_config",
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("depth_days", sa.BigInteger(), nullable=True),
        sa.Column("approved_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("skip_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(["chat_id"], ["chats.id"]),
        sa.PrimaryKeyConstraint("chat_id"),
    )


def downgrade() -> None:
    op.drop_table("chat_sync_config")
