from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, Boolean, Column, ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class Chat(Base):
    __tablename__ = "chats"

    id = Column(BigInteger, primary_key=True)
    type = Column(Text, nullable=False)
    title = Column(Text)
    username = Column(Text)
    folder = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())


class TgUser(Base):
    __tablename__ = "tg_users"

    id = Column(BigInteger, primary_key=True)
    username = Column(Text)
    first_name = Column(Text)
    last_name = Column(Text)
    language_code = Column(Text)
    is_bot = Column(Boolean, default=False)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())


class Message(Base):
    __tablename__ = "messages"

    id = Column(BigInteger, autoincrement=True, primary_key=True)
    chat_id = Column(BigInteger, ForeignKey("chats.id"), nullable=False)
    user_id = Column(BigInteger, ForeignKey("tg_users.id"))
    telegram_msg_id = Column(BigInteger)
    direction = Column(Text, nullable=False)   # 'in' | 'out'
    text = Column(Text)
    media_type = Column(Text)                  # photo/voice/document/sticker/...
    file_id = Column(Text)
    caption = Column(Text)
    raw_json = Column(JSONB)
    date_utc = Column(TIMESTAMP(timezone=True))
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    reply_to_msg_id = Column(BigInteger)
    is_auto_reply = Column(Boolean, default=False)
    via_guest_bot = Column(Boolean, default=False)
    edit_date = Column(TIMESTAMP(timezone=True))
    dialog_key = Column(Text)                  # "{chat_id}:{user_id}"

    __table_args__ = (
        Index("idx_messages_chat_date", "chat_id", "date_utc"),
        Index("idx_messages_user_date", "user_id", "date_utc"),
        Index("idx_messages_dialog_key", "dialog_key"),
        Index("idx_messages_direction", "direction"),
        UniqueConstraint("chat_id", "telegram_msg_id", name="uq_chat_msg"),
    )


class Setting(Base):
    __tablename__ = "settings"

    key = Column(Text, primary_key=True)
    value = Column(Text)


class ApiToken(Base):
    __tablename__ = "api_tokens"

    id = Column(BigInteger, autoincrement=True, primary_key=True)
    provider = Column(Text, nullable=False, default="gemini")  # gemini | openai | ...
    token = Column(Text, nullable=False)
    label = Column(Text)
    is_active = Column(Boolean, default=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    last_used_at = Column(TIMESTAMP(timezone=True))
    error_count = Column(BigInteger, default=0)


class ChatSyncConfig(Base):
    __tablename__ = "chat_sync_config"

    chat_id = Column(BigInteger, ForeignKey("chats.id"), primary_key=True)
    enabled = Column(Boolean, nullable=False, default=False)
    depth_days = Column(BigInteger, nullable=True)
    approved_at = Column(TIMESTAMP(timezone=True), nullable=True)
    skip_reason = Column(Text, nullable=True)
    last_synced_at = Column(TIMESTAMP(timezone=True), nullable=True)
    synced_depth_days = Column(BigInteger, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())


class ChatTopic(Base):
    __tablename__ = "chat_topics"

    id = Column(BigInteger, autoincrement=True, primary_key=True)
    chat_id = Column(BigInteger, ForeignKey("chats.id", ondelete="CASCADE"), nullable=False)
    topic_id = Column(BigInteger, nullable=False)
    title = Column(Text)
    is_closed = Column(Boolean, default=False)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("chat_id", "topic_id", name="uq_chat_topic"),
    )


class MessageChunk(Base):
    __tablename__ = "message_chunks"

    id = Column(BigInteger, autoincrement=True, primary_key=True)
    chat_id = Column(BigInteger, ForeignKey("chats.id"), nullable=False)
    chat_title = Column(Text)
    chunk_text = Column(Text, nullable=False)
    embedding = Column(Vector(768))
    msg_date_from = Column(TIMESTAMP(timezone=True))
    msg_date_to = Column(TIMESTAMP(timezone=True))
    max_msg_id = Column(BigInteger)          # highest messages.id in this chunk
    min_tg_msg_id = Column(BigInteger)       # lowest telegram_msg_id in this chunk
    max_tg_msg_id = Column(BigInteger)       # highest telegram_msg_id in this chunk
    chat_username = Column(Text)             # @username for link construction
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_chunks_chat", "chat_id"),
        Index("idx_chunks_date", "msg_date_from"),
    )
