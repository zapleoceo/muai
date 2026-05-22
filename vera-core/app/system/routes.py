"""Vera-core registers itself as an HTTP agent so its own self-tools
appear in the unified tool registry (no separate microservice)."""
import logging
from datetime import datetime

import httpx
from fastapi import APIRouter, HTTPException

from app.config import get_settings
from app.system.tools import HANDLERS

log = logging.getLogger(__name__)
router = APIRouter()

_AGENT_ID = "vera-self"

TOOL_SPECS = [
    {
        "name": "system_deploy",
        "description": (
            "Trigger Vera's own deploy. Dispatches the Deploy workflow on "
            "GitHub Actions which pulls latest master, builds, runs tests, "
            "rolls back on failure. Use when Dima says «задеплой» or after "
            "you've pushed changes via git MCP."
        ),
        "params": [
            {"name": "ref", "type": "string",
             "description": "Branch or tag to deploy. Default: master.",
             "required": False, "default": "master"},
            {"name": "message", "type": "string",
             "description": "Optional reason logged with the deploy.",
             "required": False, "default": ""},
        ],
    },
    {
        "name": "system_status",
        "description": (
            "Vera's own status: git HEAD on server, last 5 GitHub Actions "
            "deploy runs with their conclusion. Use when Dima asks «как "
            "ты?», «что с деплоем?», или для самопроверки после правок."
        ),
        "params": [],
    },
]


@router.post("/tool/{name}")
async def call_self_tool(name: str, payload: dict | None = None) -> dict:
    handler = HANDLERS.get(name)
    if handler is None:
        raise HTTPException(404, f"unknown self-tool {name}")
    try:
        result = await handler(**(payload or {}))
    except TypeError as exc:
        return {"ok": False, "error": f"bad args: {exc}"}
    except Exception as exc:
        log.exception("self-tool %s failed: %s", name, exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    # Match the contract HTTP tool agents use: {ok, result|error}
    if isinstance(result, dict) and "ok" in result:
        return result
    return {"ok": True, "result": result}


async def register_self_loop() -> None:
    """Register vera-core as an HTTP agent so its self-tools appear in
    collect_tools(). Register once at startup, then heartbeat every 60s."""
    import asyncio
    settings = get_settings()
    payload = {
        "id": _AGENT_ID,
        "name": "vera-self",
        "http_url": "http://localhost:8000",
        "capabilities": ["self_admin"],
        "required_caps": [],
        "tools": TOOL_SPECS,
    }
    headers = {"X-Internal-Secret": settings.internal_secret}
    # Give the FastAPI server a beat to start accepting connections.
    await asyncio.sleep(2)
    registered = False
    while True:
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                if not registered:
                    r = await c.post("http://localhost:8000/internal/agents/register",
                                     json=payload, headers=headers)
                    if r.status_code == 200:
                        registered = True
                        log.info("vera-self registered: %d tools", len(TOOL_SPECS))
                    else:
                        log.warning("self register %d: %s", r.status_code, r.text[:200])
                else:
                    await c.post("http://localhost:8000/internal/agents/heartbeat",
                                 json={"id": _AGENT_ID}, headers=headers)
        except Exception as exc:
            log.warning("self heartbeat failed: %s", exc)
        await asyncio.sleep(60)
