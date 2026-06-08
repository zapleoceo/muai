"""FastAPI приложение gateway. Минимальный API."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request

from gateway.config import get_settings
from gateway.events import router as events_router

log = logging.getLogger(__name__)


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

    app.include_router(events_router)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True, "version": "0.3.0"}

    @app.get("/")
    async def root() -> dict:
        return {"service": "vera-gateway", "version": "0.3.0"}

    return app


app = create_app()
