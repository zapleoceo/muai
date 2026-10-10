"""Фоновый цикл доставки MCP Events внутри lifespan сервиса: один дешёвый SELECT за виток."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import AsyncIterator

from vera_shared.db.engine import get_session
from vera_shared.room.event_outbox import prune_outbox

from vera_mcp.events_deliver import deliver_due

log = logging.getLogger(__name__)

POLL_S = 5.0
PRUNE_EVERY_S = 6 * 3600.0


def delivery_enabled() -> bool:
    return os.environ.get("MCP_EVENTS_DELIVERY", "1").strip() != "0"


async def _prune() -> None:
    async with get_session() as s:
        removed = await prune_outbox(s)
    if removed:
        log.info("mcp events: pruned %s outbox rows", removed)


async def run_delivery_loop(*, poll_s: float = POLL_S) -> None:
    last_prune = float("-inf")
    while True:
        try:
            await deliver_due()
            if time.monotonic() - last_prune >= PRUNE_EVERY_S:
                last_prune = time.monotonic()
                await _prune()
        except asyncio.CancelledError:
            raise
        except Exception:
            # сбой БД не должен ронять сервис: следующий виток повторит
            log.exception("mcp events: delivery loop iteration failed")
        await asyncio.sleep(poll_s)


@contextlib.asynccontextmanager
async def delivery_running() -> AsyncIterator[None]:
    if not delivery_enabled():
        log.info("mcp events: delivery disabled (MCP_EVENTS_DELIVERY=0)")
        yield
        return
    task = asyncio.create_task(run_delivery_loop(), name="mcp-events-delivery")
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
