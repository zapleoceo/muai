"""Журнал `mcp_audit`: запись в той же транзакции, что и правка, и чтение."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.engine import get_session
from vera_shared.db.models_mcp import McpAuditRow


class AuditNotFound(LookupError):
    def __init__(self, audit_id: int) -> None:
        super().__init__(f"audit entry {audit_id} not found")


async def record(
    s: AsyncSession, *, client: str, tool: str, args: dict[str, Any],
    kind: str, target_id: int | None, before: dict[str, Any] | None,
    after: dict[str, Any] | None, undo_of: int | None = None,
) -> int:
    row = McpAuditRow(client=client, tool=tool, args=args, target_kind=kind,
                      target_id=target_id, before=before, after=after,
                      undo_of=undo_of)
    s.add(row)
    await s.flush()
    return row.id


async def get_entry(s: AsyncSession, audit_id: int) -> McpAuditRow:
    row = (await s.execute(
        select(McpAuditRow).where(McpAuditRow.id == audit_id).with_for_update()
    )).scalar_one_or_none()
    if row is None:
        raise AuditNotFound(audit_id)
    return row


async def list_entries(limit: int, client: str | None = None) -> list[dict[str, Any]]:
    q = select(McpAuditRow)
    if client:
        q = q.where(McpAuditRow.client == client)
    async with get_session() as s:
        rows = (await s.execute(
            q.order_by(McpAuditRow.id.desc()).limit(limit)
        )).scalars().all()
    return [{"audit_id": r.id, "client": r.client, "tool": r.tool, "args": r.args,
             "target": f"{r.target_kind}:{r.target_id}", "status": r.status,
             "undo_of": r.undo_of, "at": r.created_at.isoformat()} for r in rows]


async def recent_rows(limit: int) -> list[McpAuditRow]:
    """Свежие записи целиком (с `before`/`after`) — для страницы журнала дашборда."""
    async with get_session() as s:
        return list((await s.execute(
            select(McpAuditRow).order_by(McpAuditRow.id.desc()).limit(limit)
        )).scalars().all())
