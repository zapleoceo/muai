"""Каталог инструментов: кэш `tools/list` апстримов и сборка списка под агента.

Кэш греет фоновая проверка здоровья; `tools/list` агента дозапрашивает только
устаревшие апстримы, параллельно и с коротким таймаутом. Апстрим, который не
ответил, просто выпадает из списка — остальные отдаются.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable
from typing import Any

from vera_shared.hub.names import MAX_TOOL_NAME, PROJECTS, allowed, tool_name

from vera_hub.errors import UpstreamError
from vera_hub.upstreams import Registry, Upstream

log = logging.getLogger(__name__)

TOOLS_TTL_S = 300.0
LIST_TIMEOUT_S = 10.0
MAX_PAGES = 10


def _prefixed(up: Upstream, tool: dict[str, Any]) -> dict[str, Any] | None:
    name = tool.get("name")
    if not isinstance(name, str) or not name:
        return None
    full = tool_name(up.project, up.slug, name)
    if len(full) > MAX_TOOL_NAME:
        log.warning("hub: tool name too long, skipped: %s/%s", up.project, up.slug)
        return None
    label = f"[{PROJECTS.get(up.project, up.project)} / {up.slug}]"
    out = dict(tool)
    out["name"] = full
    out["description"] = f"{label} {tool.get('description') or ''}".strip()
    return out


class Catalog:
    def __init__(self, registry: Registry) -> None:
        self._registry = registry
        self._tools: dict[int, tuple[Upstream, float, list[dict[str, Any]]]] = {}

    async def refresh(self, up: Upstream) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(MAX_PAGES):
            params = {"cursor": cursor} if cursor else {}
            result = await self._registry.call(up, "tools/list", params,
                                               timeout_s=min(up.timeout_s, LIST_TIMEOUT_S))
            page = result.get("tools")
            if not isinstance(page, list):
                raise UpstreamError("protocol", "tools/list returned no tools array")
            tools.extend(t for t in page if isinstance(t, dict))
            cursor = result.get("nextCursor") if isinstance(result.get("nextCursor"), str) else None
            if not cursor:
                break
        self._tools[up.id] = (up, time.monotonic(), tools)
        return tools

    def cached(self, up: Upstream) -> list[dict[str, Any]] | None:
        entry = self._tools.get(up.id)
        if entry is None or entry[0] != up or time.monotonic() - entry[1] > TOOLS_TTL_S:
            return None
        return entry[2]

    async def _ensure(self, up: Upstream) -> list[dict[str, Any]]:
        if (tools := self.cached(up)) is not None:
            return tools
        try:
            return await self.refresh(up)
        except UpstreamError as exc:
            log.warning("hub: tools/list failed for %s/%s: %s", up.project, up.slug, exc.kind)
            stale = self._tools.get(up.id)
            return stale[2] if stale and stale[0] == up else []

    async def tools_for(self, allow: Iterable[str]) -> list[dict[str, Any]]:
        grants = list(allow)
        ups = [u for u in await self._registry.upstreams()
               if u.blocker() is None and allowed(grants, u.project, u.slug)]
        lists = await asyncio.gather(*(self._ensure(u) for u in ups))
        out: list[dict[str, Any]] = []
        for up, tools in zip(ups, lists, strict=True):
            out.extend(p for t in tools if (p := _prefixed(up, t)) is not None)
        return out
