"""Фоновая проверка здоровья апстримов: initialize + tools/list, итог — в БД для админки."""
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable

from vera_shared.hub.repo_upstreams import set_health

from vera_hub.catalog import Catalog
from vera_hub.errors import UpstreamError
from vera_hub.upstreams import Registry, Upstream

log = logging.getLogger(__name__)

HealthWriter = Callable[[int, str, str, int | None], Awaitable[None]]


def interval_s() -> float:
    return max(15.0, float(os.environ.get("HUB_HEALTH_INTERVAL_S", "60")))


async def _check(up: Upstream, catalog: Catalog, write: HealthWriter) -> None:
    if (reason := up.blocker()) is not None:
        await write(up.id, reason, "", None)
        return
    try:
        tools = await catalog.refresh(up)
    except UpstreamError as exc:
        await write(up.id, "down", f"{exc.kind}: {exc}", None)
        return
    await write(up.id, "ok", "", len(tools))


async def check_all(registry: Registry, catalog: Catalog,
                    write: HealthWriter = set_health) -> None:
    ups = await registry.upstreams(fresh=True)
    results = await asyncio.gather(*(_check(u, catalog, write) for u in ups),
                                   return_exceptions=True)
    for up, res in zip(ups, results, strict=True):
        if isinstance(res, Exception):
            log.warning("hub: health check of %s/%s failed: %s", up.project, up.slug,
                        type(res).__name__)


async def health_loop(registry: Registry, catalog: Catalog) -> None:
    # Периодическая проверка по расписанию, а не ожидание условия: интервал — это и есть цель.
    while True:
        try:
            await check_all(registry, catalog)
        except Exception:
            log.exception("hub: health pass crashed")
        await asyncio.sleep(interval_s())
