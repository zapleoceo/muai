"""Очередь голосовых поручений: шлюз кладёт, бот исполняет. См. миграцию 035."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Float, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from vera_shared.db.engine import Base
from vera_shared.db.models import JsonType


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
    # Ответ уже ушёл владельцу — после перезапуска второй раз не отвечаем.
    answered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Раньше этого времени поручение не берём: ретрай с паузой, а не подряд.
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Владельцу сообщили, что поручение не выполнено. NULL при error — сообщить.
    notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Миграция 052: срочная просьба — задача в комнате и её путь до «Взял».
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="command")
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    help_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    task_opened_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    confirm_asked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    taken_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    taken_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )
