"""FastAPI приложение gateway. Минимальный API."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from gateway.config import get_settings
from gateway.events import router as events_router

log = logging.getLogger(__name__)

# Защита от 100MB JSON атаки. Реальные события: gmail max 8000 chars text +
# metadata ~ 50KB. 2MB более чем достаточно.
MAX_BODY_BYTES = int(os.environ.get("GATEWAY_MAX_BODY_BYTES", str(2 * 1024 * 1024)))


class MaxBodySizeMiddleware(BaseHTTPMiddleware):
    """Отвергает POST с Content-Length > MAX_BODY_BYTES до парсинга."""

    async def dispatch(self, request: Request, call_next):
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                if int(cl) > MAX_BODY_BYTES:
                    return Response(
                        f"payload too large (> {MAX_BODY_BYTES} bytes)",
                        status_code=413,
                    )
            except ValueError:
                pass
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

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True, "version": "0.3.0"}

    @app.get("/")
    async def root() -> dict:
        return {"service": "vera-gateway", "version": "0.3.0"}

    return app


app = create_app()
