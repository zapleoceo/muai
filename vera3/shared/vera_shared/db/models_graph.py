"""L1/L2/L3 graph layer materialized in Postgres.

Behind `graph_repo` API so Neo4j swap is a one-file change later.

- L1 Reality: entities, entity_aliases, memberships, relationships
- L2 Patterns: patterns
- L3 Identity: identity_nodes (goal/value/nogo/style/self)
"""
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
    Text,
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


# ─── L1 Reality ──────────────────────────────────────────────────────────────


class EntityRow(Base):
    """A resolved real-world thing — person, chat, channel, project, place."""
    __tablename__ = "entities"
    __table_args__ = (
        Index("ix_entities_type_name", "type", "name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(40), nullable=False)
    # person | group | channel | account | project | place | label | other
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    canonical_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Optional stable label across rebuilds (e.g. "person:dima_zaporozhets")
    attributes: Mapped[dict[str, Any]] = mapped_column(_JsonType, nullable=False, default=dict)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )


class EntityAliasRow(Base):
    """Identity resolution: each external identifier → one Entity.

    Example: ('telegram', 'user:169510539'), ('gmail', 'demoniwwwe@gmail.com'),
    ('instagram', 'user:zapleo_ceo') all alias the SAME entity (Dima).
    """
    __tablename__ = "entity_aliases"
    __table_args__ = (
        UniqueConstraint("source", "identifier", name="uq_alias_source_identifier"),
        Index("ix_alias_entity", "entity_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), nullable=False,
    )
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    identifier: Mapped[str] = mapped_column(String(500), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)


class MembershipRow(Base):
    """Who is in what. parent = group/channel/account, child = person."""
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("parent_entity_id", "child_entity_id", "source",
                         name="uq_membership"),
        Index("ix_membership_parent", "parent_entity_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    parent_entity_id: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), nullable=False,
    )
    child_entity_id: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), nullable=False,
    )
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    role: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # creator | admin | member | follower | following | participant
    attributes: Mapped[dict[str, Any]] = mapped_column(_JsonType, nullable=False, default=dict)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class RelationshipRow(Base):
    """Edge between two entities with a fact string (Graphiti-style).

    Examples:
      (Dima)  -[CO_FOUNDER_OF, since=2023]->  (Veranda)
      (Dima)  -[BOSS_AT,      role=Director]-> (IT STEP Jakarta)
      (Vasya) -[WIFE_OF]->                     (Petya)
    """
    __tablename__ = "relationships"
    __table_args__ = (
        Index("ix_rel_subject", "subject_entity_id"),
        Index("ix_rel_object", "object_entity_id"),
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
    derived_from_event_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


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
