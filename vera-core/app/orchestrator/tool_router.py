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


async def collect_tools() -> tuple[list[dict], dict[str, str]]:
    """Return (tool_specs, tool_name → bot_http_url). Only fresh agents."""
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
    route: dict[str, str] = {}
    for a in agents:
        for t in (a.tools or []):
            specs.append(t)
            route[t["name"]] = a.http_url
    return specs, route


async def call_tool(route: dict[str, str], name: str, args: dict) -> dict:
    url = route.get(name)
    if url is None:
        return {"ok": False, "error": f"unknown tool '{name}'"}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(f"{url}/tool/{name}", json=args)
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
