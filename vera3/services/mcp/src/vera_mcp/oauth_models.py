"""Durable, room-scoped OAuth state. Secret values are never stored in cleartext."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from vera_shared.db.engine import Base


class RoomOAuthClient(Base):
    __tablename__ = "room_oauth_clients"

    client_id: Mapped[str] = mapped_column(String(512), primary_key=True)
    encrypted_info: Mapped[str] = mapped_column(Text, nullable=False)


class RoomOAuthGrant(Base):
    __tablename__ = "room_oauth_grants"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    client_id: Mapped[str] = mapped_column(String(512), nullable=False)
    actor: Mapped[str | None] = mapped_column(String(64))
    data_json: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    family_id: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        Index("ix_room_oauth_grants_client", "client_id"),
        Index("ix_room_oauth_grants_family", "family_id"),
        Index("ix_room_oauth_grants_expires", "expires_at"),
    )
