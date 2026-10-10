"""MCP Events: подписки на события задач комнаты и транзакционный outbox (миграция 053)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from vera_shared.db.engine import Base
from vera_shared.db.models import BigIntPk, JsonType


class RoomEventSubscriptionRow(Base):
    """Подписка принципала; секрет хранится зашифрованным (Fernet, ключ ROOM_OAUTH_KEY)."""
    __tablename__ = "room_event_subscriptions"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'revoked')", name="ck_room_event_subs_status"),
        Index("ix_room_event_subs_match", "room", "event_name", "status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    principal: Mapped[str] = mapped_column(String(64), nullable=False)
    room: Mapped[str] = mapped_column(String(64), nullable=False)
    event_name: Mapped[str] = mapped_column(String(16), nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    kinds: Mapped[list[Any]] = mapped_column(JsonType, nullable=False, default=list)
    callback_url: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_secret: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class RoomEventOutboxRow(Base):
    """Пишется в той же транзакции, что и событие задачи; доставка идёт отдельно."""
    __tablename__ = "room_event_outbox"
    __table_args__ = (
        UniqueConstraint("subscription_id", "event_id", name="uq_room_event_outbox_event"),
        CheckConstraint("status IN ('pending', 'delivered', 'dropped', 'revoked', 'failed')",
                        name="ck_room_event_outbox_status"),
        Index("ix_room_event_outbox_due", "status", "next_attempt_at"),
        Index("ix_room_event_outbox_task", "subscription_id", "task_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    subscription_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("room_event_subscriptions.id", ondelete="CASCADE"), nullable=False)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_name: Mapped[str] = mapped_column(String(16), nullable=False)
    task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now())
    last_error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now())
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    source_event_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
