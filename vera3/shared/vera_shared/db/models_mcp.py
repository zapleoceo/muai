"""Журнал изменений, сделанных агентами через удалённый MCP (миграция 036)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from vera_shared.db.engine import Base
from vera_shared.db.models import BigIntPk, JsonType


class McpAuditRow(Base):
    """Одна запись на каждую запись агента в мозг; по `before` откатывается `undo`.

    `target_kind`/`target_id` — что изменено (event, entity, alias,
    relationship). `status`: applied → undone; запись самого отката хранит
    `undo_of` и не откатывается повторно.
    """
    __tablename__ = "mcp_audit"
    __table_args__ = (
        Index("ix_mcp_audit_created_at", "created_at"),
        Index("ix_mcp_audit_target", "target_kind", "target_id"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    client: Mapped[str] = mapped_column(String(64), nullable=False)
    tool: Mapped[str] = mapped_column(String(64), nullable=False)
    args: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    target_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    before: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="applied")
    undo_of: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )
