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

from app.bot.handler import router as bot_router
from app.bot.sender import init_bot
from app.config import get_settings
from app.deploy.endpoint import router as deploy_router
from app.internal.agents import router as agents_router

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

    _bot = Bot(
        token=settings.telegram_bot_token_vera,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    init_bot(_bot)

    _dp = Dispatcher()
    _dp.include_router(bot_router)

    webhook_url = f"{settings.webhook_base_url}/bot/webhook"
    await _bot.set_webhook(webhook_url, drop_pending_updates=True)
    log.info("Webhook set to %s", webhook_url)

    yield

    await _bot.delete_webhook()
    await _bot.session.close()
    log.info("Bot shutdown complete")


app = FastAPI(title="vera-core", lifespan=lifespan)
app.include_router(deploy_router)
app.include_router(agents_router)


@app.post("/bot/webhook")
async def bot_webhook(request: Request) -> dict:
    if _dp is None or _bot is None:
        return {"ok": False}
    body = await request.body()
    update = Update.model_validate(json.loads(body))
    await _dp.feed_update(bot=_bot, update=update)
    return {"ok": True}
