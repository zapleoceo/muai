"""ORM models — соответствуют Pydantic схемам из vera_shared.events/tokens.

Принцип: ORM-модели (TokenRow, EventRow) — для БД.
Pydantic-модели (Token, RawEvent) — для бизнес-логики и API.
Маппинг через `to_dict()` / `from_dict()`.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

# Используем JSONB на Postgres (быстрее, индексируется), JSON на других (для тестов SQLite)
JsonType = JSONB().with_variant(JSON(), "sqlite")

# BigInteger PK не работает с SQLite autoincrement — используем Integer вариант
BigIntPk = BigInteger().with_variant(Integer(), "sqlite")

from vera_shared.db.engine import Base


class TokenRow(Base):
    """Table tokens — API ключи провайдеров с tier и cost caps."""

    __tablename__ = "tokens"
    __table_args__ = (
        UniqueConstraint("provider", "label", name="uq_token_provider_label"),
        Index("ix_tokens_provider_active", "provider", "is_active"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    tier: Mapped[str] = mapped_column(String(10), nullable=False, default="free")
    capabilities: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    daily_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=999_999)
    daily_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    daily_cost_cap_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    daily_cost_used_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    monthly_cost_cap_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    monthly_cost_used_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total_cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    daily_reset_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )


class SourceRow(Base):
    """Table sources — настроенные источники данных."""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    connector_type: Mapped[str] = mapped_column(String(50), nullable=False)
    credentials_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    config: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_event_cursor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )


class EventRow(Base):
    """Table events — все события сырыми. Источник истины."""

    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint(
            "source", "source_event_id",
            name="uq_event_source_id",
        ),
        Index("ix_events_occurred_at", "occurred_at"),
        Index("ix_events_source", "source"),
        Index("ix_events_account", "account"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    source_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    account: Mapped[str | None] = mapped_column(String(255), nullable=True)
    category: Mapped[str] = mapped_column(String(50), nullable=False, default="generic")
    content_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    content_extra: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    entity_hints: Mapped[list[dict[str, Any]]] = mapped_column(
        JsonType, nullable=False, default=list,
    )
    metadata_: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", JsonType, nullable=True,
    )

    # Времена
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )

    # Embedding — pgvector. Создаётся отдельно через init_pgvector migration.
    # Можем хранить как bytes (raw) или использовать pgvector type.
    # Для гибкости — JSONB пока, pgvector type подключим в SQL миграции.
    embedding_voyage_3: Mapped[list[float] | None] = mapped_column(
        JsonType, nullable=True,
    )

    # Triage metadata (results от brain-triage)
    triage_metadata: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    importance: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Graphiti reference (если попало в граф)
    graphiti_episode_uuid: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # Processing state
    triage_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending",
    )  # pending | processing | done | error
    triage_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Когда воркер захватил это событие в processing — для watchdog.
    # NULL значит pending/done — never claimed.
    triage_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class JobRow(Base):
    """Table jobs — backfill / consolidation / reflection runs."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(50), nullable=False)  # backfill | consolidation | ...
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending",
    )  # pending | running | done | error | cancelled
    config: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    progress: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )


class UsageLogRow(Base):
    """Table usage_log — каждый LLM-вызов трейсится здесь.

    Используется cost reconciliation jobs для сверки с реальным billing.
    """

    __tablename__ = "usage_log"
    __table_args__ = (
        Index("ix_usage_token_date", "token_id", "created_at"),
        Index("ix_usage_provider_date", "provider", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    token_id: Mapped[int | None] = mapped_column(
        ForeignKey("tokens.id", ondelete="SET NULL"), nullable=True,
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    capability: Mapped[str] = mapped_column(String(30), nullable=False)
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    error_kind: Mapped[str | None] = mapped_column(String(50), nullable=True)
    workflow: Mapped[str | None] = mapped_column(String(50), nullable=True)
    event_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )
