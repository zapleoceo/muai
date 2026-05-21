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
    try:
        args = await _resolve_safe_args(name, args)
    except _ResolveError as exc:
        return {"ok": False, "error": f"arg resolution: {exc}"}
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


class _ResolveError(Exception):
    pass


async def _resolve_safe_args(name: str, args: dict) -> dict:
    """For destructive tools (send_*, send_reply, modify_*) certain args are
    NOT trusted from the LLM. We re-derive them from authoritative state
    (e.g. recipient = last sender of the actual thread). Prevents prompt-
    injection via email content tricking Vera into emailing attacker."""
    if name == "gmail_send_reply":
        email = args.get("email")
        thread_id = args.get("thread_id")
        if not email or not thread_id:
            raise _ResolveError("gmail_send_reply requires email + thread_id")
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                resp = await c.post(
                    "http://vera-gmail:8004/tool/gmail_read_thread",
                    json={"email": str(email), "thread_id": str(thread_id),
                          "ocr_images": False},
                )
            resp.raise_for_status()
            data = resp.json().get("result") or {}
            messages = data.get("messages") or []
            inbound = [m for m in messages if (m.get("from") or "").strip()]
            if not inbound:
                raise _ResolveError("no inbound messages in thread")
            last_from = inbound[-1].get("from") or ""
            import re as _re
            m = _re.search(r"<([^>]+)>", last_from) or _re.search(r"\b[\w.+-]+@[\w.-]+\.\w+\b", last_from)
            authoritative_to = (m.group(1) if hasattr(m, 'group') and m and "<" in last_from else
                                m.group(0) if m else "").strip()
            if not authoritative_to:
                raise _ResolveError(f"could not parse 'to' from sender {last_from!r}")
            if args.get("to") and args["to"].lower() != authoritative_to.lower():
                log.warning("gmail_send_reply: overriding LLM-chosen to=%r with thread sender %r",
                            args.get("to"), authoritative_to)
            args = dict(args)
            args["to"] = authoritative_to
        except _ResolveError:
            raise
        except Exception as exc:
            raise _ResolveError(f"could not resolve recipient: {exc}")
    return args


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
