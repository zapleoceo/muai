"""Комната агентов: сообщения, задачи с арендой и курсоры чтения (миграция 047)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Index, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from vera_shared.db.engine import Base
from vera_shared.db.models import BigIntPk, JsonType


class RoomMessageRow(Base):
    """Только дописывается; (room, message_id) — ключ идемпотентности клиента."""
    __tablename__ = "room_messages"
    __table_args__ = (
        UniqueConstraint("room", "message_id", name="uq_room_messages_message_id"),
        Index("ix_room_messages_room_id", "room", "id"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    room: Mapped[str] = mapped_column(String(64), nullable=False)
    message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    from_agent: Mapped[str] = mapped_column(String(64), nullable=False)
    to_agent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    in_reply_to: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="info")
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now())


class RoomTaskRow(Base):
    """Каждый новый захват увеличивает fencing_token — устаревший держатель отвергается."""
    __tablename__ = "room_tasks"

    room: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    lease_holder: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    paths: Mapped[list[Any]] = mapped_column(JsonType, nullable=False, default=list)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now())


class RoomCursorRow(Base):
    __tablename__ = "room_cursors"

    agent: Mapped[str] = mapped_column(String(64), primary_key=True)
    room: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now())
