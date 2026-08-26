"""FastAPI приложение gateway. Минимальный API."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from gateway.claude import router as claude_router
from gateway.claude_session import router as claude_session_router
from gateway.config import get_settings
from gateway.events import router as events_router
from gateway.query import router as query_router
from gateway.voice import router as voice_router

log = logging.getLogger(__name__)

# Защита от 100MB JSON атаки. Реальные события: gmail max 8000 chars text +
# metadata ~ 50KB. 2MB более чем достаточно.
MAX_BODY_BYTES = int(os.environ.get("GATEWAY_MAX_BODY_BYTES", str(2 * 1024 * 1024)))


class MaxBodySizeMiddleware(BaseHTTPMiddleware):
    """Отвергает POST с Content-Length > MAX_BODY_BYTES до парсинга."""

    async def dispatch(self, request: Request, call_next):
        cl = request.headers.get("content-length")
        if cl is None:
            # Chunked transfer обходит проверку размера — все легитимные
            # клиенты (httpx json=) шлют Content-Length, отклоняем остальных.
            if request.method in ("POST", "PUT", "PATCH"):
                return Response("Content-Length required", status_code=411)
            return await call_next(request)
        try:
            if int(cl) > MAX_BODY_BYTES:
                return Response(
                    f"payload too large (> {MAX_BODY_BYTES} bytes)",
                    status_code=413,
                )
        except ValueError:
            return Response("invalid Content-Length", status_code=400)
        return await call_next(request)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup / shutdown — DB engine init."""
    from vera_shared.db.engine import close_engine, init_engine
    settings = get_settings()
    await init_engine(settings.database_url)
    log.info("Gateway started, DB connected")
    yield
    await close_engine()


def create_app() -> FastAPI:
    """Factory pattern — для лёгкого тестирования."""
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)

    app = FastAPI(
        title="Vera 3.0 Gateway",
        version="0.3.0",
        lifespan=lifespan,
    )
    app.add_middleware(MaxBodySizeMiddleware)

    app.include_router(events_router)
    app.include_router(claude_router)
    app.include_router(claude_session_router)
    app.include_router(query_router)
    app.include_router(voice_router)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True, "version": "0.3.0"}

    @app.get("/")
    async def root() -> dict:
        return {"service": "vera-gateway", "version": "0.3.0"}

    return app


app = create_app()
