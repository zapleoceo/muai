"""SQL журнала /mcp. Только метаданные запроса — тел и заголовков тут нет."""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from vera_shared.db.engine import get_session

_INSERT = text(
    "INSERT INTO mcp_request_log (cid, method, actor, ua, rpc, tool, rpc_id, status, ms, outcome) "
    "VALUES (:cid, :method, :actor, :ua, :rpc, :tool, :rpc_id, :status, :ms, :outcome)")


async def insert_request(row: dict[str, Any]) -> None:
    async with get_session() as session:
        await session.execute(_INSERT, row)
