"""Фоновый цикл сторожа задач: раз в минуту, из lifespan дашборда."""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from vera_shared.room.watchdog import run_once

log = logging.getLogger(__name__)
INTERVAL_S = 60


async def watchdog_loop(interval_s: float = INTERVAL_S) -> None:
    while True:
        try:
            await run_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("watchdog pass crashed")
        await asyncio.sleep(interval_s)


def start_watchdog() -> asyncio.Task[None]:
    return asyncio.create_task(watchdog_loop(), name="room-watchdog")


async def stop_watchdog(task: asyncio.Task[None]) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
