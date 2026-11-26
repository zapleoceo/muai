"""MCP-хаб (миграция 064): апстримы по проектам, агенты с правами, журнал вызовов."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from vera_shared.db.engine import Base
from vera_shared.db.models import BigIntPk, JsonType


class HubUpstreamRow(Base):
    """Один MCP-сервер за хабом. `secret_enc` — только шифротекст (HUB_SECRET_KEY)."""
    __tablename__ = "hub_upstreams"
    __table_args__ = (UniqueConstraint("project", "slug", name="uq_hub_upstreams_project_slug"),)

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    project: Mapped[str] = mapped_column(String(16), nullable=False)
    slug: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    transport: Mapped[str] = mapped_column(String(8), nullable=False, default="http")
    url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    command: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)
    auth_kind: Mapped[str] = mapped_column(String(8), nullable=False, default="none")
    auth_header: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    hint: Mapped[str] = mapped_column(Text, nullable=False, default="")
    timeout_s: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    max_concurrency: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    health: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    health_error: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    health_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    tool_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())


class HubAgentRow(Base):
    """Агент хаба. Хранится sha256 токена, сам токен показывается один раз."""
    __tablename__ = "hub_agents"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    token_prefix: Mapped[str] = mapped_column(String(12), nullable=False, default="")
    allow: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class HubCallRow(Base):
    """Метаданные вызова: без аргументов, результатов и секретов."""
    __tablename__ = "hub_calls"
    __table_args__ = (Index("ix_hub_calls_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    agent: Mapped[str] = mapped_column(String(32), nullable=False)
    method: Mapped[str] = mapped_column(String(32), nullable=False)
    tool: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    project: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    upstream: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())


def upstream_dict(row: HubUpstreamRow) -> dict[str, Any]:
    """Для админки: всё, кроме шифротекста секрета (только флаг `has_secret`)."""
    return {
        "id": row.id, "project": row.project, "slug": row.slug, "title": row.title,
        "transport": row.transport, "url": row.url, "command": list(row.command or []),
        "auth_kind": row.auth_kind, "auth_header": row.auth_header,
        "has_secret": bool(row.secret_enc), "enabled": row.enabled, "hint": row.hint,
        "timeout_s": row.timeout_s, "max_concurrency": row.max_concurrency,
        "health": row.health, "health_error": row.health_error, "health_at": row.health_at,
        "tool_count": row.tool_count, "updated_at": row.updated_at,
    }
