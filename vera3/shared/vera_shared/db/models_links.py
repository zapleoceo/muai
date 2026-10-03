"""Связи событий с сущностями, прозвища, карта голосов (миграции 042–044)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from vera_shared.db.engine import Base

_JsonType = JSON().with_variant(JSONB(), "postgresql")
_BigIntPk = BigInteger().with_variant(Integer(), "sqlite")


class EventEntityRow(Base):
    """Одна связь «событие ↔ сущность» (042): роль, откуда известно, уверенность."""
    __tablename__ = "event_entities"
    __table_args__ = (Index("ix_event_entities_entity", "entity_id", "role", "event_id"),)

    event_id: Mapped[int] = mapped_column(_BigIntPk, ForeignKey("events.id", ondelete="CASCADE"),
                                          primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id", ondelete="CASCADE"),
                                           primary_key=True)
    role: Mapped[str] = mapped_column(String(12), primary_key=True)
    token: Mapped[str] = mapped_column(String(120), primary_key=True, default="")
    source_of_link: Mapped[str] = mapped_column(String(12), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    span: Mapped[dict[str, Any] | None] = mapped_column(_JsonType, nullable=True)
    scope_ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                 server_default=func.now())


class LinkCursorRow(Base):
    """До какого events.id построен индекс связей (042), по потоку forward / backfill."""
    __tablename__ = "link_cursor"

    name: Mapped[str] = mapped_column(String(20), primary_key=True)
    event_id: Mapped[int] = mapped_column(_BigIntPk, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                 server_default=func.now())


class EntityNicknameRow(Base):
    """Прозвище или инициалы с областью действия (043); suggested ждёт владельца."""
    __tablename__ = "entity_nicknames"
    __table_args__ = (UniqueConstraint("entity_id", "token", name="uq_entity_nickname"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id", ondelete="CASCADE"),
                                           nullable=False)
    token: Mapped[str] = mapped_column(String(80), nullable=False)
    case_sensitive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    scope_kind: Mapped[str] = mapped_column(String(12), nullable=False, default="work")
    scope_ids: Mapped[list[Any]] = mapped_column(_JsonType, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="suggested")
    source: Mapped[str] = mapped_column(String(12), nullable=False, default="owner")
    reason: Mapped[str] = mapped_column(String, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                 server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class VoiceSpeakerMapRow(Base):
    """Ярлык голоса → сущность (044): kind 'event' (`<event_id>:<ярлык>`) или 'voiceprint'."""
    __tablename__ = "voice_speaker_map"
    __table_args__ = (Index("ix_voice_speaker_map_entity", "entity_id"),)

    kind: Mapped[str] = mapped_column(String(12), primary_key=True)
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id", ondelete="CASCADE"),
                                           nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                 server_default=func.now())
