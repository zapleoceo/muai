"""Tool ABC + spec for the agent loop.

Each Tool has a stable name, description (LLM-readable), JSON-Schema
params, and an exec coroutine. Tools are registered into a single
ToolRegistry that brain-search uses for function-calling.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Awaitable


@dataclass
class ToolSpec:
    """JSON-serializable spec, sent to LLM as a function definition."""
    name: str
    description: str
    params_schema: dict[str, Any]   # JSON Schema (OpenAI/Anthropic compatible)


class Tool(ABC):
    """A capability the agent can invoke."""

    spec: ToolSpec

    @abstractmethod
    async def exec(self, **params: Any) -> dict[str, Any]:
        """Execute the tool. Returns JSON-serializable result."""


class FunctionTool(Tool):
    """Adapter that wraps an async function into a Tool."""

    def __init__(self, spec: ToolSpec,
                 fn: Callable[..., Awaitable[dict[str, Any]]]) -> None:
        self.spec = spec
        self._fn = fn

    async def exec(self, **params: Any) -> dict[str, Any]:
        return await self._fn(**params)
