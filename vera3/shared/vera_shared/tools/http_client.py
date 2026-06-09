"""HTTPTool — invokes a tool on another vera service over HTTP.

Each ingestor publishes /tools/{name} endpoints; brain-search registers
HTTPTool wrappers and the LLM cannot tell the difference between local
and remote tools.
"""
from __future__ import annotations

import os
from typing import Any

import httpx

from vera_shared.tools.base import Tool, ToolSpec


INTERNAL_SECRET = os.environ.get("INTERNAL_SECRET", "")


class HTTPTool(Tool):
    """Forward a tool call to {base_url}/tools/{name}."""

    def __init__(self, spec: ToolSpec, base_url: str,
                 timeout: float = 30.0) -> None:
        self.spec = spec
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def exec(self, **params: Any) -> dict[str, Any]:
        url = f"{self.base_url}/tools/{self.spec.name.split('.', 1)[-1]}"
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.post(
                url, json=params,
                headers={"X-Internal-Secret": INTERNAL_SECRET},
            )
        if r.status_code >= 400:
            return {"error": f"HTTP {r.status_code}", "body": r.text[:500]}
        return r.json()
