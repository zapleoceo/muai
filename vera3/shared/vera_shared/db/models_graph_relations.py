"""Graph L1 edges: relationships, pair stats, connection suppressions and
identity merge suggestions (see `models_graph`)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
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
from sqlalchemy.orm import Mapped, mapped_column

from vera_shared.db.engine import Base


class RelationshipRow(Base):
    """Edge between two entities with a fact string.

    Examples:
      (Dima)  -[CO_FOUNDER_OF, since=2023]->  (Veranda)
      (Dima)  -[BOSS_AT,      role=Director]-> (IT STEP Jakarta)
      (Vasya) -[WIFE_OF]->                     (Petya)
    """
    __tablename__ = "relationships"
    __table_args__ = (
        Index("ix_rel_subject", "subject_entity_id"),
        Index("ix_rel_object", "object_entity_id"),
        # то же, что миграция 017: на проде индекс есть, в схему тестов его
        # приносит только модель
        Index("uq_relationships_spo", "subject_entity_id", "predicate",
              "object_entity_id", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subject_entity_id: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), nullable=False,
    )
    object_entity_id: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), nullable=False,
    )
    predicate: Mapped[str] = mapped_column(String(80), nullable=False)
    fact: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.6)
    derived_from_event_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("events.id", ondelete="SET NULL"), nullable=True,
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class PairStatsRow(Base):
    """Кэш взаимодействий пары сущностей (миграция 040): считается из событий
    периодической задачей brain-triage и целиком пересобирается, правды тут нет —
    источник остаётся в `events`. Пара упорядочена: entity_a < entity_b."""
    __tablename__ = "pair_stats"
    __table_args__ = (
        Index("ix_pair_stats_b", "entity_b"),
    )

    entity_a: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True,
    )
    entity_b: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True,
    )
    dm_msgs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dm_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mail_msgs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mail_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    co_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    work_co_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    co_chats: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    shared_groups: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )


class ConnectionSuppressionRow(Base):
    """Пара, у которой владелец отверг ВЫВЕДЕННУЮ роль «работает с» (миграция 041).

    У выведенной роли нет строк `relationships`, гасить в ней нечего; запись здесь
    говорит модели связи: общение этой пары не считать уликой роли. Пара упорядочена:
    entity_a < entity_b."""
    __tablename__ = "connection_suppressions"
    __table_args__ = (
        UniqueConstraint("entity_a", "entity_b", "predicate", name="uq_connection_suppression"),
    )

    # Суррогатный id нужен слиянию сущностей: отчёт `MergeReport` ищет строку по одному ключу.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_a: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), nullable=False,
    )
    entity_b: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), nullable=False,
    )
    predicate: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )


class MergeSuggestionRow(Base):
    """Vera's LLM verdict on a candidate same-person pair (migration 016).

    Owner accepts (→ merge_entities) or rejects on /entities/duplicates;
    'different' verdicts are stored pre-rejected so a pair is never re-asked.
    (entity_a, entity_b) is ordered a<b — unique either way round.
    """
    __tablename__ = "merge_suggestions"
    __table_args__ = (
        UniqueConstraint("entity_a", "entity_b", name="uq_merge_pair"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_a: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), nullable=False,
    )
    entity_b: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), nullable=False,
    )
    verdict: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )
