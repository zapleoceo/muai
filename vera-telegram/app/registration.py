import asyncio
import logging

import httpx

from app.config import get_settings
from app.tool_specs import TOOLS

AGENT_ID = "vera-telegram"
AGENT_NAME = "Telegram Userbot"
HTTP_URL = "http://vera-telegram:8001"

logger = logging.getLogger(__name__)

_HEARTBEAT_INTERVAL = 30
_REGISTER_RETRY = 10


def _headers() -> dict:
    return {"X-Internal-Secret": get_settings().internal_secret}


async def register_self() -> bool:
    cfg = get_settings()
    payload = {
        "id": AGENT_ID,
        "name": AGENT_NAME,
        "http_url": HTTP_URL,
        "tools": [t.to_dict() for t in TOOLS],
    }
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{cfg.vera_core_url}/internal/agents/register",
            json=payload, headers=_headers(),
        )
        resp.raise_for_status()
    logger.info("Registered with vera-core (%d tools)", len(TOOLS))
    return True


async def _heartbeat() -> None:
    cfg = get_settings()
    async with httpx.AsyncClient(timeout=5) as client:
        await client.post(
            f"{cfg.vera_core_url}/internal/agents/heartbeat",
            json={"id": AGENT_ID}, headers=_headers(),
        )


async def register_loop() -> None:
    for attempt in range(1, 6):
        try:
            await register_self()
            break
        except Exception as exc:
            logger.warning("Registration attempt %d failed: %s", attempt, exc)
            await asyncio.sleep(_REGISTER_RETRY)
    while True:
        await asyncio.sleep(_HEARTBEAT_INTERVAL)
        try:
            await _heartbeat()
        except Exception as exc:
            logger.warning("Heartbeat failed: %s", exc)
