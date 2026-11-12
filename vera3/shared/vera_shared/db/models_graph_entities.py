"""Graph L1: entities, avatars, aliases and memberships (see `models_graph`)."""
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
    LargeBinary,
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


class EntityAvatarRow(Base):
    """Profile photo for an entity — filled lazily+throttled by the userbot.

    Blob kept out of the hot `entities` table (see migration 014). `missing`
    marks 'checked, no photo' so backfill won't re-hammer Telegram.
    """
    __tablename__ = "entity_avatars"

    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True,
    )
    image: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    mime: Mapped[str] = mapped_column(Text, nullable=False, default="image/jpeg")
    missing: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    fetched_at: Mapped[datetime] = mapped_column(
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
