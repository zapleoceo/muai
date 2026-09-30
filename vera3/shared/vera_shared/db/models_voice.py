"""Очередь голосовых поручений: шлюз кладёт, бот исполняет. См. миграцию 035."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from vera_shared.db.engine import Base


class VoiceCommandRow(Base):
    """Table voice_command_queue — поручение ждёт ответа владельцу в Telegram."""

    __tablename__ = "voice_command_queue"
    __table_args__ = (Index("ix_voice_command_status", "status", "created_at"),)

    # Ключ выдаёт слушатель: ретрай той же команды из его очереди не задвоит её.
    command_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    instruction: Mapped[str] = mapped_column(Text, nullable=False, default="")
    spoken_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # «Услышала, делаю» уже ушло — ретрай после сбоя шлёт только результат.
    acked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )
