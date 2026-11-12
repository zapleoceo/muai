"""Graph L2 patterns and L3 identity nodes (see `models_graph`)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
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

# JSONB на Postgres (быстрее, индексируется), JSON на SQLite (тесты) — тот же
# паттерн что в vera_shared/db/models.py::JsonType. Без варианта тесты, чей
# create_all() задевает Base.metadata целиком (напр. gateway service tests),
# падают CompileError'ом на SQLite, даже не трогая эти таблицы напрямую.
_JsonType = JSONB().with_variant(JSON(), "sqlite")


# ─── L2 Patterns ─────────────────────────────────────────────────────────────


class PatternRow(Base):
    """Observed (trigger, action) pair with feedback weight.

    Example: trigger="@boss writes after 22:00, urgent words"
             action="suggest immediate read aloud"
             weight grows on Dima 👍, drops on ✋ Откати.
    """
    __tablename__ = "patterns"
    __table_args__ = (
        UniqueConstraint("trigger_signature", "action_kind", name="uq_pattern"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trigger_signature: Mapped[str] = mapped_column(String(500), nullable=False)
    action_kind: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(_JsonType, nullable=False, default=dict)
    observation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    correction_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )


# ─── L3 Identity ─────────────────────────────────────────────────────────────


class IdentityNodeRow(Base):
    """Goal / Value / NoGo / Style / Self — who Dima is, what he wants."""
    __tablename__ = "identity_nodes"
    __table_args__ = (
        Index("ix_identity_type_listener", "type", "listener_entity_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(40), nullable=False)
    # goal | value | nogo | style | self | preference | fact
    label: Mapped[str] = mapped_column(String(500), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(_JsonType, nullable=False, default=dict)
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    listener_entity_id: Mapped[int | None] = mapped_column(
        ForeignKey("entities.id", ondelete="SET NULL"), nullable=True,
    )
    # For 'style' nodes — recipient/listener.
    derived_from: Mapped[dict[str, Any]] = mapped_column(_JsonType, nullable=False, default=dict)
    # {tool_calls: [...], events: [...], conversations: [...]}
    valid_from: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.7)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now(),
    )
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
