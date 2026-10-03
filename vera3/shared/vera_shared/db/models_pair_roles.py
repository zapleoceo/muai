"""Роли пары, выведенные моделью из переписки (миграция 045): результат и последний прогон."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from vera_shared.db.engine import Base

_JsonType = JSON().with_variant(JSONB(), "postgresql")


class PairRoleInferenceRow(Base):
    """Роль пары, выведенная моделью из переписки. Пара упорядочена: a < b;
    direction — a_to_b | b_to_a | both. Производные данные, не правка владельца."""
    __tablename__ = "pair_role_inferences"
    __table_args__ = (Index("ix_pair_role_inferences_b", "entity_b"),)

    entity_a: Mapped[int] = mapped_column(ForeignKey("entities.id", ondelete="CASCADE"),
                                          primary_key=True)
    entity_b: Mapped[int] = mapped_column(ForeignKey("entities.id", ondelete="CASCADE"),
                                          primary_key=True)
    predicate: Mapped[str] = mapped_column(String(80), primary_key=True)
    direction: Mapped[str] = mapped_column(String(8), primary_key=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    quotes: Mapped[list[str]] = mapped_column(_JsonType, nullable=False, default=list)
    model: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                  server_default=func.now())


class PairRoleRunRow(Base):
    """Последний прогон модели по паре: хэш пакета улик, маркер статистики, резюме, цена."""
    __tablename__ = "pair_role_runs"
    __table_args__ = (Index("ix_pair_role_runs_computed", "computed_at"),)

    entity_a: Mapped[int] = mapped_column(ForeignKey("entities.id", ondelete="CASCADE"),
                                          primary_key=True)
    entity_b: Mapped[int] = mapped_column(ForeignKey("entities.id", ondelete="CASCADE"),
                                          primary_key=True)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    pair_marker: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    roles_found: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    model: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    computed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                  server_default=func.now())
