"""HTTP-обвязка хаба: `POST /hub/mcp` (MCP Streamable HTTP, stateless, JSON) и `/healthz`."""
from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from vera_shared.db.engine import close_engine, init_engine

from vera_hub.auth import AgentAuth
from vera_hub.calls import drain_calls
from vera_hub.catalog import Catalog
from vera_hub.health import health_loop
from vera_hub.rpc import INVALID_REQUEST, PARSE_ERROR, HubRpc, error
from vera_hub.upstreams import Registry

MCP_PATH = "/hub/mcp"
MAX_REQUEST = 1024 * 1024
_NO_STORE = {"Cache-Control": "no-store"}
_CHALLENGE = {"WWW-Authenticate": "Bearer", **_NO_STORE}


def _deny(status: int, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status, headers=headers or _NO_STORE)


def build_app(registry: Registry | None = None, auth: AgentAuth | None = None,
              background: bool = True) -> Starlette:
    reg = registry or Registry()
    catalog = Catalog(reg)
    rpc = HubRpc(reg, catalog)
    gate = auth or AgentAuth()

    async def mcp_endpoint(request: Request) -> Response:
        # агенты — не браузеры: Origin значит чужую страницу (DNS rebinding, CSRF)
        if request.headers.get("origin"):
            return _deny(403, "browser origins are not allowed")
        agent = await gate.resolve(request.headers.get("authorization"))
        if agent is None:
            return _deny(401, "unauthorized", _CHALLENGE)
        if request.method != "POST":
            return Response(status_code=405, headers={"Allow": "POST", **_NO_STORE})
        body = await request.body()
        if len(body) > MAX_REQUEST:
            return _deny(413, "request too large")
        try:
            msg = json.loads(body)
        except (ValueError, RecursionError):
            return JSONResponse(error(None, PARSE_ERROR, "parse error"), headers=_NO_STORE)
        if isinstance(msg, list):
            return JSONResponse(error(None, INVALID_REQUEST, "batching is not supported"),
                                headers=_NO_STORE)
        reply = await rpc.handle(agent, msg)
        if reply is None:
            return Response(status_code=202, headers=_NO_STORE)
        return JSONResponse(reply, headers=_NO_STORE)

    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        await init_engine()
        task = asyncio.create_task(health_loop(reg, catalog)) if background else None
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await drain_calls()
            await reg.close()
            await close_engine()

    return Starlette(routes=[
        Route(MCP_PATH, mcp_endpoint, methods=["GET", "POST", "DELETE"]),
        Route("/healthz", healthz, methods=["GET"]),
    ], lifespan=lifespan)
