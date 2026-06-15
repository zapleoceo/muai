import inspect
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from app.tool_handlers import HANDLERS
from app.tool_specs import TOOLS
from vera_shared.tools.spec import ToolSpec

log = logging.getLogger(__name__)

_PY_TYPES: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
}

DESTRUCTIVE = {"telegram_delete_messages", "telegram_clear_history"}


def _make_tool(spec: ToolSpec):
    handler = HANDLERS[spec.name]

    async def impl(**kwargs: Any) -> Any:
        try:
            return await handler(**kwargs)
        except (LookupError, ValueError, TypeError) as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}

    params = []
    annotations: dict[str, type] = {}
    for p in spec.params:
        py_type = _PY_TYPES.get(p.type, str)
        default = inspect.Parameter.empty if p.required else p.default
        params.append(
            inspect.Parameter(
                p.name,
                inspect.Parameter.KEYWORD_ONLY,
                default=default,
                annotation=py_type,
            )
        )
        annotations[p.name] = py_type
    impl.__signature__ = inspect.Signature(params)  # type: ignore[attr-defined]
    impl.__annotations__ = {**annotations, "return": Any}
    impl.__name__ = spec.name
    return impl


def register_tools(mcp: FastMCP, allow_destructive: bool) -> list[str]:
    registered: list[str] = []
    for spec in TOOLS:
        if spec.name not in HANDLERS:
            continue
        if spec.name in DESTRUCTIVE and not allow_destructive:
            continue
        mcp.add_tool(_make_tool(spec), name=spec.name, description=spec.description)
        registered.append(spec.name)
    log.info("MCP registered %d telegram tools: %s", len(registered), registered)
    return registered
