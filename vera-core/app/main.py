import json
import logging
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Update
from fastapi import FastAPI, Request

from vera_shared.db.engine import init_engine
from vera_shared.db.migrations import run_migrations

from app.bot.callbacks import router as callbacks_router
from app.bot.handler import router as bot_router
from app.bot.sender import init_bot
from app.config import get_settings
from app.dashboard.api import router as dashboard_api_router
from app.dashboard.static import router as dashboard_static_router
from app.deploy.endpoint import router as deploy_router
from app.events.routes import router as events_router
from app.gmail.routes import router as gmail_router
from app.graph.routes import router as graph_router
from app.internal.agents import router as agents_router
from app.mcp.routes import router as mcp_router
from app.persona.routes import router as persona_router
from app.admin.routes import router as admin_router
from app.sources.routes import router as sources_router
from app.self_extend.routes import router as self_extend_router

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

_bot: Bot | None = None
_dp: Dispatcher | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _bot, _dp
    settings = get_settings()

    engine = await init_engine()
    await run_migrations(engine)

    from vera_shared.tokens.repository import migrate_plaintext_tokens
    migrated = await migrate_plaintext_tokens()
    if migrated:
        log.info("Encrypted %d plaintext tokens at rest", migrated)

    # Boot MCP servers from DB; failures here must not crash vera-core
    try:
        from app.mcp.manager import refresh_from_db
        await refresh_from_db()
    except Exception as exc:
        log.exception("MCP boot failed: %s", exc)

    _bot = Bot(
        token=settings.telegram_bot_token_vera,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    init_bot(_bot)

    _dp = Dispatcher()
    _dp.include_router(callbacks_router)  # match callbacks before message handler
    _dp.include_router(bot_router)

    webhook_url = f"{settings.webhook_base_url}/bot/webhook"
    await _bot.set_webhook(
        webhook_url, drop_pending_updates=True,
        allowed_updates=["message", "edited_message", "callback_query",
                         "my_chat_member", "chat_join_request"],
    )
    log.info("Webhook set to %s", webhook_url)

    yield

    try:
        from app.mcp.manager import stop_all
        await stop_all()
    except Exception as exc:
        log.warning("MCP stop_all failed: %s", exc)

    await _bot.delete_webhook()
    await _bot.session.close()
    log.info("Bot shutdown complete")


app = FastAPI(title="vera-core", lifespan=lifespan)
app.include_router(deploy_router)
app.include_router(agents_router)
app.include_router(events_router)
app.include_router(gmail_router)
app.include_router(graph_router)
app.include_router(mcp_router)
app.include_router(persona_router)
app.include_router(admin_router)
app.include_router(sources_router)
app.include_router(self_extend_router)
app.include_router(dashboard_api_router)
app.include_router(dashboard_static_router)


@app.post("/bot/webhook")
async def bot_webhook(request: Request) -> dict:
    if _dp is None or _bot is None:
        return {"ok": False}
    body = await request.body()
    update = Update.model_validate(json.loads(body))
    await _dp.feed_update(bot=_bot, update=update)
    return {"ok": True}
