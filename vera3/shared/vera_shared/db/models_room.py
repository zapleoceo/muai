"""Комната агентов: сообщения, задачи с арендой и курсоры чтения (миграции 047, 049)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from vera_shared.db.engine import Base
from vera_shared.db.models import BigIntPk, JsonType


class RoomMessageRow(Base):
    """Только дописывается; (room, message_id) — ключ идемпотентности клиента."""
    __tablename__ = "room_messages"
    __table_args__ = (
        UniqueConstraint("room", "message_id", name="uq_room_messages_message_id"),
        Index("ix_room_messages_room_id", "room", "id"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    room: Mapped[str] = mapped_column(String(64), nullable=False)
    message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    from_agent: Mapped[str] = mapped_column(String(64), nullable=False)
    from_session: Mapped[str | None] = mapped_column(String(128), nullable=True)
    to_agent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    in_reply_to: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="info")
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now())


class RoomTaskRow(Base):
    """Каждый новый захват увеличивает fencing_token — устаревший держатель отвергается."""
    __tablename__ = "room_tasks"

    room: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    lease_holder: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    paths: Mapped[list[Any]] = mapped_column(JsonType, nullable=False, default=list)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now())
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=2)
    owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    next_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    refs: Mapped[list[Any]] = mapped_column(JsonType, nullable=False, default=list)
    holder_session: Mapped[str | None] = mapped_column(String(128), nullable=True)
    holder_account: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_progress_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_progress_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_checkpoint_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    pending_handoff_to: Mapped[str | None] = mapped_column(String(64), nullable=True)
    plan_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    plan_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class RoomCursorRow(Base):
    __tablename__ = "room_cursors"

    agent: Mapped[str] = mapped_column(String(64), primary_key=True)
    room: Mapped[str] = mapped_column(String(64), primary_key=True)
    consumer: Mapped[str] = mapped_column(String(64), primary_key=True, default="default")
    last_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now())


EVENT_KINDS = (
    "created", "claimed", "progress", "heartbeat", "paused", "resumed", "review", "blocked",
    "unblocked", "question", "answered", "ack_answer", "handoff_offer", "handoff_accept",
    "released", "done", "lease_expired", "watchdog_action",
)
_KINDS_SQL = ", ".join(f"'{k}'" for k in EVENT_KINDS)


class RoomTaskEventRow(Base):
    """Только дописывается: журнал того, что происходило с задачей."""
    __tablename__ = "room_task_events"
    __table_args__ = (
        CheckConstraint(f"kind IN ({_KINDS_SQL})", name="ck_room_task_events_kind"),
        Index("ix_room_task_events_task", "room", "task_id", "id"),
        Index("ix_room_task_events_kind_at", "kind", "at"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    room: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    agent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    session: Mapped[str | None] = mapped_column(String(128), nullable=True)
    account: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fencing_token: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    data: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)


class RoomTaskQuestionRow(Base):
    __tablename__ = "room_task_questions"
    __table_args__ = (
        CheckConstraint("status IN ('open', 'answered', 'acked', 'withdrawn')",
                        name="ck_room_task_questions_status"),
        Index("ix_room_task_questions_open", "room", "task_id",
              postgresql_where=text("status = 'open'")),
    )

    qid: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    room: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    asked_by: Mapped[str] = mapped_column(String(64), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    asked_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now())
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    acked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ack_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


class RoomTaskAnswerRow(Base):
    __tablename__ = "room_task_answers"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    qid: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("room_task_questions.qid"), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    answered_by: Mapped[str] = mapped_column(String(64), nullable=False)
    answered_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now())


class WatchdogStateRow(Base):
    __tablename__ = "watchdog_state"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
