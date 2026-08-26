"""Дополнительные модели для source-specific state."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from vera_shared.db.engine import Base


class GmailAccountRow(Base):
    """Gmail OAuth account для polling."""
    __tablename__ = "gmail_accounts"
    __table_args__ = (
        UniqueConstraint("email", name="uq_gmail_email"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    refresh_token_enc: Mapped[str] = mapped_column(Text, nullable=False)
    access_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_expiry: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    history_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    include_automated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Статус OAuth: токен отозван Google → нужен повторный consent.
    needs_reauth: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )


class TelegramSessionRow(Base):
    """Telegram MTProto session info (one userbot)."""
    __tablename__ = "telegram_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    phone: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    session_string_enc: Mapped[str] = mapped_column(Text, nullable=False)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )


class InstagramSessionRow(Base):
    """Instagram mobile-API session (instagrapi). Cookies+device JSON."""
    __tablename__ = "instagram_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    session_json_enc: Mapped[str] = mapped_column(Text, nullable=False)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_thread_cursor: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )


class SlackConversationRow(Base):
    """Курсор опроса одного канала / лички / группы Slack.

    Токен живёт в env (личный, один на всё), поэтому здесь только состояние
    обхода. `last_ts` — время последнего разобранного сообщения строкой Slack
    («1756123456.001200»): оно же и есть идентификатор сообщения в канале, так
    что курсор не теряет хвост при всплеске активности.
    """
    __tablename__ = "slack_conversations"

    conversation_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="channel")
    is_private: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_ts: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )


class SlackThreadRow(Base):
    """Тред, за которым продолжаем следить.

    Нужна отдельно от каналов, потому что ответы в тредах НЕ приходят в
    `conversations.history` — история отдаёт только корневое сообщение. Больше
    того, тред, чьё корневое сообщение старше курсора, в истории не появится
    вовсе, сколько бы новых ответов в нём ни было. Без этой таблицы обсуждения
    в Slack — а решения принимаются именно в них — были бы невидимы навсегда.
    """
    __tablename__ = "slack_threads"
    __table_args__ = (
        UniqueConstraint("conversation_id", "thread_ts", name="uq_slack_thread"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(String(32), nullable=False)
    thread_ts: Mapped[str] = mapped_column(String(32), nullable=False)
    last_reply_ts: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )


class TrelloBoardRow(Base):
    """Курсор опроса одной доски Trello.

    Токен и ключ живут в env (личный аккаунт, один на всё), поэтому здесь
    только состояние обхода: докуда дошли и что сломалось в прошлый раз.
    """
    __tablename__ = "trello_boards"

    board_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    last_action_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )
