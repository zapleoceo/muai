"""Office intake: происхождение задач внешнего источника и ключи идемпотентности ответов (055)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from vera_shared.db.engine import Base
from vera_shared.db.models import BigIntPk, JsonType


class RoomTaskOriginRow(Base):
    __tablename__ = "room_task_origin"
    __table_args__ = (
        UniqueConstraint("creator_principal", "client_task_id", name="uq_room_task_origin_client"),
        Index("ix_room_task_origin_creator", "creator_principal", "room", "task_id"),
    )

    room: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    creator_principal: Mapped[str] = mapped_column(String(64), nullable=False)
    client_task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    origin: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    result_recipient: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    context: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    attachments: Mapped[list[Any]] = mapped_column(JsonType, nullable=False, default=list)
    correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    causation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    delivered_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now())


class OfficeAnswerKeyRow(Base):
    __tablename__ = "office_answer_keys"
    __table_args__ = (
        UniqueConstraint("task_id", "idempotency_key", name="uq_office_answer_keys"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    room: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    qid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    revision_after: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now())
