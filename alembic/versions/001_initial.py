"""initial schema

Revision ID: 001
Revises:
Create Date: 2025-01-01 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "chats",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "tg_users",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.Text(), nullable=True),
        sa.Column("first_name", sa.Text(), nullable=True),
        sa.Column("last_name", sa.Text(), nullable=True),
        sa.Column("language_code", sa.Text(), nullable=True),
        sa.Column("is_bot", sa.Boolean(), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "settings",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("telegram_msg_id", sa.BigInteger(), nullable=True),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("media_type", sa.Text(), nullable=True),
        sa.Column("file_id", sa.Text(), nullable=True),
        sa.Column("caption", sa.Text(), nullable=True),
        sa.Column("raw_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("date_utc", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("reply_to_msg_id", sa.BigInteger(), nullable=True),
        sa.Column("is_auto_reply", sa.Boolean(), nullable=True),
        sa.Column("via_guest_bot", sa.Boolean(), nullable=True),
        sa.Column("edit_date", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("dialog_key", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["chat_id"], ["chats.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["tg_users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chat_id", "telegram_msg_id", name="uq_chat_msg"),
    )
    op.create_index("idx_messages_chat_date", "messages", ["chat_id", "date_utc"])
    op.create_index("idx_messages_user_date", "messages", ["user_id", "date_utc"])
    op.create_index("idx_messages_dialog_key", "messages", ["dialog_key"])
    op.create_index("idx_messages_direction", "messages", ["direction"])


def downgrade() -> None:
    op.drop_table("messages")
    op.drop_table("settings")
    op.drop_table("tg_users")
    op.drop_table("chats")
