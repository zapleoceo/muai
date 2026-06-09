"""ToolRegistry — single source of truth for what the agent can call.

Tools register at import-time. brain-search asks the registry for
JSON-Schema specs and an executor by name.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from vera_shared.tools.base import Tool, ToolSpec

log = logging.getLogger(__name__)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        name = tool.spec.name
        if name in self._tools:
            log.warning("Tool %s already registered — overwriting", name)
        self._tools[name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def specs(self, prefix: str | None = None) -> list[ToolSpec]:
        return [t.spec for n, t in self._tools.items()
                if prefix is None or n.startswith(prefix)]

    def all_names(self) -> list[str]:
        return list(self._tools.keys())

    async def exec(self, name: str, params: dict[str, Any],
                    timeout: float = 30.0) -> dict[str, Any]:
        tool = self.get(name)
        if tool is None:
            return {"error": f"unknown tool: {name}",
                    "available": self.all_names()[:20]}
        try:
            return await asyncio.wait_for(tool.exec(**params), timeout=timeout)
        except asyncio.TimeoutError:
            return {"error": f"tool {name} timed out after {timeout}s"}
        except Exception as e:
            log.exception("tool %s failed", name)
            return {"error": f"{type(e).__name__}: {e}"}


# Process-wide default registry; services can also create local ones.
_default = ToolRegistry()


def default_registry() -> ToolRegistry:
    return _default
