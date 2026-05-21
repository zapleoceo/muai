import json
import logging
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models import Agent

log = logging.getLogger(__name__)
_TIMEOUT = 60.0
_STALE_AFTER = timedelta(minutes=5)


async def collect_tools() -> tuple[list[dict], dict[str, tuple[str, str]]]:
    """Return (tool_specs, tool_name → (kind, target)).
    kind = 'http' → target is bot http_url
    kind = 'mcp'  → target is MCP server name
    HTTP tools (fresh agents) come first; MCP tools augment them.
    On name collisions, HTTP wins (so manual adapter overrides MCP)."""
    fresh_after = datetime.utcnow() - _STALE_AFTER
    async with get_session() as session:
        result = await session.execute(
            select(Agent).where(
                Agent.status == "online",
                Agent.last_heartbeat.isnot(None),
                Agent.last_heartbeat >= fresh_after,
            )
        )
        agents = result.scalars().all()

    specs: list[dict] = []
    route: dict[str, tuple[str, str]] = {}
    seen: set[str] = set()
    for a in agents:
        for t in (a.tools or []):
            n = t["name"]
            if n in seen:
                continue
            seen.add(n)
            specs.append(t)
            route[n] = ("http", a.http_url)

    # MCP
    try:
        from app.mcp.manager import get_routed_tools
        mcp_routed = await get_routed_tools()
        for tool_name, (server_name, spec) in mcp_routed.items():
            if tool_name in seen:
                continue
            seen.add(tool_name)
            specs.append(spec)
            route[tool_name] = ("mcp", server_name)
    except Exception as exc:
        log.warning("collect MCP tools failed: %s", exc)
    return specs, route


async def call_tool(route: dict[str, tuple[str, str]], name: str, args: dict) -> dict:
    target = route.get(name)
    if target is None:
        return {"ok": False, "error": f"unknown tool '{name}'"}
    kind, dest = target
    if kind == "mcp":
        from app.mcp.manager import call_tool as mcp_call
        return await mcp_call(dest, name, args)
    # http
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(f"{dest}/tool/{name}", json=args)
            resp.raise_for_status()
            return resp.json()
    except httpx.TimeoutException:
        return {"ok": False, "error": f"timeout after {_TIMEOUT}s"}
    except Exception as exc:
        log.warning("Tool %s failed: %s", name, exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def format_tools_for_prompt(specs: list[dict]) -> str:
    if not specs:
        return "(no tools available)"
    lines = []
    for s in specs:
        params = ", ".join(
            f"{p['name']}: {p['type']}" + (" (optional)" if not p.get('required', True) else "")
            for p in s.get("params", [])
        )
        lines.append(f"- {s['name']}({params})\n    {s['description']}")
    return "\n".join(lines)


def truncate_for_llm(obj: Any, max_chars: int = 8000) -> str:
    s = json.dumps(obj, ensure_ascii=False, default=str)
    if len(s) <= max_chars:
        return s
    return s[:max_chars] + f"\n…(truncated, total {len(s)} chars)"
