"""add username to chats

Revision ID: 004
Revises: 003
Create Date: 2025-05-11
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chats", sa.Column("username", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("chats", "username")
