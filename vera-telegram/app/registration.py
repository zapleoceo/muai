import asyncio
import logging

import httpx

from app.bot import AGENT_ID, AGENT_NAME, CAPABILITIES, REQUIRED_CAPS, HTTP_URL
from app.config import get_settings

logger = logging.getLogger(__name__)

_HEARTBEAT_INTERVAL = 30
_REGISTER_RETRY = 10


async def register_self() -> bool:
    cfg = get_settings()
    payload = {
        "id": AGENT_ID,
        "name": AGENT_NAME,
        "capabilities": CAPABILITIES,
        "required_caps": REQUIRED_CAPS,
        "http_url": HTTP_URL,
    }
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(f"{cfg.vera_core_url}/api/agents/register", json=payload)
        resp.raise_for_status()
    logger.info("Registered with vera-core as %s", AGENT_ID)
    return True


async def _heartbeat() -> None:
    cfg = get_settings()
    async with httpx.AsyncClient(timeout=5) as client:
        await client.post(f"{cfg.vera_core_url}/api/agents/{AGENT_ID}/heartbeat")


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
