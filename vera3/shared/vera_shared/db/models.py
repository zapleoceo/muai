"""ORM models — соответствуют Pydantic схемам из vera_shared.events/tokens.

Принцип: ORM-модели (EventRow, UsageLogRow) — для БД.
Pydantic-модели (Token, RawEvent) — для бизнес-логики и API.
Маппинг через `to_dict()` / `from_dict()`.
"""
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

# NOTE: TokenRow / `tokens` table removed 2026-06-29. Vera holds no LLM
# provider keys — every chat/embed/vision/transcribe call goes through the
# broker (aib.zapleo.com), which owns all keys. See migration 008.
#
# NOTE: SourceRow / `sources` table removed 2026-08-26. Ни один ингестор
# никогда её не читал и не писал: состояние обхода живёт в per-source таблицах
# (gmail_accounts, telegram_sessions, instagram_sessions, trello_boards,
# slack_conversations), а секреты — в infra/.env либо в *_sessions под crypto.
# Вместе с ней удалены две мёртвые ABC источника (vera_shared/sources,
# vera_shared/connectors) — их тоже не реализовал никто. См. миграцию 023.


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
        Index("ix_events_project", "project"),
        Index("ix_events_nature", "nature"),
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

    # Embedding вынесен в отдельную таблицу event_embeddings (миграция 011).
    # Колонка events.embedding_voyage_3 удалена из БД (VACUUM FULL) — ORM-атрибут
    # тоже убран, иначе любой SELECT EventRow падает на несуществующей колонке.

    # Triage metadata (results от brain-triage)
    triage_metadata: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    importance: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Классификация природы и принадлежности — пишется триажем.
    # nature: world_event | my_intent | conversation_with_me | derived_fact
    # project: itstep | veranda | family | personal | news | other
    nature: Mapped[str | None] = mapped_column(String(24), nullable=True)
    project: Mapped[str | None] = mapped_column(String(24), nullable=True)

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

    # Ready status subtype (null | 'deal' | 'openhouse')
    # 'deal' = lead ready to BUY (has contact, purchase intent, within cohort)
    # 'openhouse' = lead ready to ATTEND (June 29 event)
    ready_subtype: Mapped[str | None] = mapped_column(String(20), nullable=True)


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


class EventEmbeddingRow(Base):
    """Voyage-эмбеддинг события — вынесен из events (миграция 011).

    Отдельная узкая таблица: событий ~396k × ~6.5КБ вектор = ~2.5ГБ. Держать
    их inline в events заставляло каждый COUNT/GROUP BY читать 3.9ГБ. Здесь
    поиск джойнит по event_id только когда реально нужен вектор.
    """
    __tablename__ = "event_embeddings"

    event_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("events.id", ondelete="CASCADE"), primary_key=True,
    )
    embedding: Mapped[list[float]] = mapped_column(JsonType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )


class ProjectMembershipRow(Base):
    """Принадлежность чата/человека/аккаунта к проекту.

    Источник истины — папки Telegram + правила имён (см. projects/rules.py).
    Синхронизируется sync_projects.py.
    """
    __tablename__ = "project_membership"
    __table_args__ = (
        Index("ix_pm_kind_key", "kind", "key"),
    )

    project: Mapped[str] = mapped_column(String(24), primary_key=True)
    kind: Mapped[str] = mapped_column(String(10), primary_key=True)  # chat|person|account
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(60), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )


class UsageLogRow(Base):
    """Table usage_log — каждый LLM-вызов трейсится здесь.

    Используется cost reconciliation jobs для сверки с реальным billing.
    """

    __tablename__ = "usage_log"
    __table_args__ = (
        Index("ix_usage_provider_date", "provider", "created_at"),
        Index("ix_usage_event", "event_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
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
    # Из ответа брокера — трейс конкретного запроса к aibroker.
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    key_label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )


class AppControlRow(Base):
    """Table app_control — runtime key/value flags polled by workers
    (e.g. backfill_paused). See vera_shared.control + migration 009."""

    __tablename__ = "app_control"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )


class ClaudeSessionQueueRow(Base):
    """Table claude_session_queue — сессия Claude Code ждёт осмысления.

    Осмыслить её в самом запросе нельзя: одно окно не укладывается и в 120с
    ожидания брокера, а большая сессия это до 20 окон. Шлюз принимает и
    отвечает 202, фоновый воркер осмысляет. См. миграцию 026.
    """

    __tablename__ = "claude_session_queue"
    __table_args__ = (Index("ix_claude_queue_status", "status", "created_at"),)

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_dir: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    cwd: Mapped[str | None] = mapped_column(Text, nullable=True)
    git_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ended_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # Сырая переписка живёт здесь ТОЛЬКО до осмысления — воркер её очищает.
    turns: Mapped[list[dict[str, Any]]] = mapped_column(
        JsonType, nullable=False, default=list,
    )
    turn_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    done_turns: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )
