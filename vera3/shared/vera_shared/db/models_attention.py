"""Внимание владельца и очередь отложенных уведомлений (миграция 054)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, SmallInteger, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from vera_shared.db.engine import Base
from vera_shared.db.models import BigIntPk


class OwnerAttentionRow(Base):
    """Одна строка (id=1): состояние переживает рестарт бота."""
    __tablename__ = "owner_attention"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_owner_attention_single"),
        CheckConstraint("state IN ('active', 'wait10', 'quiet')", name="ck_owner_attention_state"),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    state: Mapped[str] = mapped_column(String(8), nullable=False, default="active")
    wait_question_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    wait_deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    quiet_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now())


class OwnerDeferredRow(Base):
    """Уведомление, не отправленное в тишине; dedup_key уникален, повтор обновляет строку."""
    __tablename__ = "owner_deferred"
    __table_args__ = (Index("ix_owner_deferred_pending", "sent_at", "id"),)

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    dedup_key: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    room: Mapped[str | None] = mapped_column(String(64), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    question_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
