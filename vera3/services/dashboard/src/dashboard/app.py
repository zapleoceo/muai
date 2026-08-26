"""Vera 3.0 dashboard — FastAPI app factory + router wiring.

Each page/feature area lives in its own `*_routes.py` module (see
docs/architecture.md for the full list); this file only owns app
bootstrap, favicon serving, and mounting every router.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from vera_shared.db.engine import close_engine, init_engine

from dashboard.auth_routes import router as auth_router
from dashboard.entities_routes import router as entities_router
from dashboard.events_routes import router as events_router
from dashboard.gmail_oauth import router as gmail_oauth_router
from dashboard.graph_routes import router as graph_router
from dashboard.home_routes import router as home_router
from dashboard.instagram_login import router as instagram_login_router
from dashboard.progress_routes import router as progress_router
from dashboard.render import FAVICON_SVG
from dashboard.search_routes import router as search_router
from dashboard.settings_routes import router as settings_router
from dashboard.slack_connect import router as slack_connect_router
from dashboard.sources_routes import router as sources_router
from dashboard.telegram_login import router as telegram_login_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await init_engine()
    yield
    await close_engine()


app = FastAPI(title="Vera 3.0 Dashboard", lifespan=lifespan)

for router in (
    auth_router, home_router, progress_router, events_router,
    sources_router, search_router, settings_router, entities_router,
    graph_router, gmail_oauth_router, instagram_login_router,
    telegram_login_router, slack_connect_router,
):
    app.include_router(router)


@app.get("/favicon.svg")
async def favicon_svg() -> Response:
    return Response(content=FAVICON_SVG, media_type="image/svg+xml",
                     headers={"Cache-Control": "public, max-age=86400"})


@app.get("/favicon.ico")
async def favicon_ico() -> Response:
    # Modern browsers (2020+) accept image/svg+xml at .ico paths — avoids
    # 404 spam in dev consoles without shipping a separate bitmap.
    return Response(content=FAVICON_SVG, media_type="image/svg+xml",
                     headers={"Cache-Control": "public, max-age=86400"})
