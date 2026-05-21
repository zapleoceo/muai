"""MCP client manager: connects to registered MCP servers (stdio/sse/http)
and exposes their tools through our unified Tool Registry.

Current state: skeleton. Wiring happens in S1.1 when first real server
(fetch-mcp for smoke test) is added. The manager is safe to import and
call list_servers() / refresh() — they will no-op if no servers configured.
"""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select, update

from vera_shared.db.engine import get_session
from vera_shared.db.models import MCPServer

log = logging.getLogger(__name__)


@dataclass
class MCPServerHandle:
    """In-memory view of one connected MCP server."""
    id: int
    name: str
    transport: str  # stdio | sse | http
    tools: list[dict] = field(default_factory=list)  # tool specs in our format
    status: str = "stopped"
    error: str | None = None
    # underlying client session — populated when the mcp Python SDK is wired up
    _session: Any = None


_servers: dict[str, MCPServerHandle] = {}
_lock = asyncio.Lock()


async def list_servers() -> list[MCPServerHandle]:
    return list(_servers.values())


async def get_routed_tools() -> dict[str, tuple[str, dict]]:
    """Return {tool_name: (server_name, tool_spec)} for all running servers."""
    out: dict[str, tuple[str, dict]] = {}
    for h in _servers.values():
        if h.status != "running":
            continue
        for t in h.tools:
            out[t["name"]] = (h.name, t)
    return out


async def call_tool(server_name: str, tool_name: str, args: dict) -> dict:
    """Route a tool call to the named MCP server. Returns {ok, result|error}."""
    handle = _servers.get(server_name)
    if not handle:
        return {"ok": False, "error": f"MCP server '{server_name}' not registered"}
    if handle.status != "running":
        return {"ok": False, "error": f"MCP server '{server_name}' status={handle.status}"}
    if handle._session is None:
        return {"ok": False, "error": "MCP server not yet wired (S1.1 pending)"}

    # Actual mcp SDK call goes here (mcp.client.session.call_tool).
    # Placeholder: until wiring, refuse cleanly.
    return {"ok": False, "error": "MCP transport not yet implemented in S1 skeleton"}


async def refresh_from_db() -> None:
    """Reconcile in-memory _servers with DB rows. Called on startup + after
    dashboard add/remove. Does not actually start connections yet — that's S1.1.
    """
    async with _lock:
        async with get_session() as session:
            result = await session.execute(
                select(MCPServer).where(MCPServer.enabled == True)
            )
            rows = result.scalars().all()

        seen_names = {r.name for r in rows}
        for name in list(_servers):
            if name not in seen_names:
                await _stop(name)

        for row in rows:
            if row.name not in _servers:
                _servers[row.name] = MCPServerHandle(
                    id=row.id, name=row.name, transport=row.transport,
                    status="stopped",
                )
            # In S1.1 we'd dispatch _start(handle) here.


async def _stop(name: str) -> None:
    h = _servers.pop(name, None)
    if h and h._session is not None:
        try:
            await h._session.close()  # mcp SDK signature
        except Exception as exc:
            log.warning("MCP session close failed for %s: %s", name, exc)


async def mark_status(name: str, status: str, error: str | None = None,
                      tools_count: int | None = None) -> None:
    async with get_session() as session:
        stmt = (
            update(MCPServer)
            .where(MCPServer.name == name)
            .values(status=status, error_message=error,
                    last_started_at=datetime.utcnow() if status == "running" else None)
        )
        if tools_count is not None:
            stmt = stmt.values(tools_count=tools_count)
        await session.execute(stmt)
        await session.commit()
