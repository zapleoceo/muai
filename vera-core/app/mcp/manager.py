"""MCP client manager: connects to registered MCP servers (stdio/sse/http)
and exposes their tools through our unified Tool Registry."""
import asyncio
import logging
import os
from contextlib import AsyncExitStack
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
    transport: str
    tools: list[dict] = field(default_factory=list)
    status: str = "stopped"
    error: str | None = None
    _session: Any = None
    _exit_stack: AsyncExitStack | None = None


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
    if handle.status != "running" or handle._session is None:
        return {"ok": False, "error": f"MCP server '{server_name}' not running ({handle.status})"}

    try:
        result = await handle._session.call_tool(tool_name, arguments=args or {})
        parts = []
        for c in (result.content or []):
            if hasattr(c, "text") and c.text:
                parts.append(c.text)
            elif hasattr(c, "data"):
                parts.append({"binary_bytes": len(c.data or b"")})
        is_error = bool(getattr(result, "isError", False))
        flat = "\n".join(p for p in parts if isinstance(p, str)) or parts
        await _record_tool_call(server_name, is_error=is_error,
                                error_text=(flat if is_error else None))
        return {"ok": not is_error, "result": flat}
    except Exception as exc:
        log.warning("MCP call %s/%s failed: %s", server_name, tool_name, exc)
        await _record_tool_call(server_name, is_error=True, error_text=str(exc))
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


_AUTH_ERROR_PATTERNS = ("401", "403", "unauthorized", "invalid token",
                       "token expired", "authentication failed",
                       "access denied", "permission denied")


async def _record_tool_call(server_name: str, *, is_error: bool,
                            error_text: str | None = None) -> None:
    try:
        from datetime import datetime
        from sqlalchemy import update
        from vera_shared.db.models import MCPServer
        async with get_session() as session:
            stmt = (
                update(MCPServer)
                .where(MCPServer.name == server_name)
                .values(
                    tool_calls_count=MCPServer.tool_calls_count + 1,
                    last_tool_call_at=datetime.utcnow(),
                )
            )
            await session.execute(stmt)
            if is_error and error_text:
                low = (error_text or "").lower()
                if any(p in low for p in _AUTH_ERROR_PATTERNS):
                    await session.execute(
                        update(MCPServer)
                        .where(MCPServer.name == server_name)
                        .values(auth_state="token_expired")
                    )
            await session.commit()
        if is_error and error_text:
            low = (error_text or "").lower()
            if any(p in low for p in _AUTH_ERROR_PATTERNS):
                try:
                    from app.self_extend.token_watcher import notify_token_expired
                    await notify_token_expired(server_name)
                except Exception:
                    pass
    except Exception as exc:
        log.debug("record_tool_call failed: %s", exc)


async def refresh_from_db() -> None:
    """Reconcile in-memory _servers with DB rows. Starts/stops as needed."""
    async with _lock:
        async with get_session() as session:
            result = await session.execute(
                select(MCPServer).where(MCPServer.enabled == True)
            )
            rows = result.scalars().all()

        seen_names = {r.name for r in rows}
        # Stop removed/disabled
        for name in list(_servers):
            if name not in seen_names:
                await _stop(name)

        # Start newly added
        for row in rows:
            if row.name in _servers:
                continue
            try:
                await _start(row)
            except Exception as exc:
                log.exception("MCP start failed for %s: %s", row.name, exc)
                _servers[row.name] = MCPServerHandle(
                    id=row.id, name=row.name, transport=row.transport,
                    status="error", error=str(exc),
                )
                await mark_status(row.name, "error", error=str(exc))


async def _start(row: MCPServer) -> None:
    """Spin up subprocess (stdio) or open SSE/HTTP and grab tools."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    handle = MCPServerHandle(
        id=row.id, name=row.name, transport=row.transport, status="starting",
    )
    _servers[row.name] = handle
    await mark_status(row.name, "starting")

    stack = AsyncExitStack()
    try:
        if row.transport == "stdio":
            cmd = row.command or []
            if not cmd:
                raise ValueError("stdio server has empty command")
            params = StdioServerParameters(
                command=cmd[0], args=cmd[1:],
                env={**os.environ, **(row.env or {})},
            )
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            tools_resp = await session.list_tools()
            handle._session = session
            handle._exit_stack = stack
            handle.tools = [_normalise_tool(t) for t in (tools_resp.tools or [])]
            handle.status = "running"
            await mark_status(row.name, "running", tools_count=len(handle.tools))
            log.info("MCP %s started: %d tools", row.name, len(handle.tools))
        else:
            raise NotImplementedError(f"transport {row.transport} not yet supported")
    except Exception:
        await stack.aclose()
        handle.status = "error"
        raise


def _normalise_tool(t: Any) -> dict:
    """Convert mcp.types.Tool to our HTTP-adapter-style spec."""
    schema = getattr(t, "inputSchema", None) or {}
    props = (schema.get("properties") or {}) if isinstance(schema, dict) else {}
    required = set((schema.get("required") or []) if isinstance(schema, dict) else [])
    params = []
    for name, prop in props.items():
        params.append({
            "name": name,
            "type": prop.get("type", "string") if isinstance(prop, dict) else "string",
            "description": (prop.get("description") if isinstance(prop, dict) else "") or "",
            "required": name in required,
        })
    return {
        "name": t.name,
        "description": (getattr(t, "description", "") or "")[:500],
        "params": params,
    }


async def _stop(name: str) -> None:
    h = _servers.pop(name, None)
    if h and h._exit_stack is not None:
        try:
            await h._exit_stack.aclose()
        except Exception as exc:
            log.warning("MCP exit_stack close failed for %s: %s", name, exc)
    await mark_status(name, "stopped")


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


async def stop_all() -> None:
    for name in list(_servers):
        await _stop(name)
