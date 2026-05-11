import logging
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.bot.handlers import commands, messages as msg_handlers
from app.config import get_settings
from app.db.database import engine
from app.db.models import Base

settings = get_settings()

logging.basicConfig(
    level=getattr(logging, settings.log_level),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

bot = Bot(
    token=settings.telegram_bot_token,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()
dp.include_router(commands.router)
dp.include_router(msg_handlers.router)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("DB tables ready")

    if settings.webhook_url:
        await bot.set_webhook(
            url=settings.webhook_url,
            secret_token=settings.webhook_secret or None,
            allowed_updates=["message", "edited_message", "callback_query",
                             "my_chat_member", "chat_join_request"],
            drop_pending_updates=False,
        )
        logger.info("Webhook set: %s", settings.webhook_url)
    else:
        logger.warning("WEBHOOK_URL not set")

    from app.userbot.client import start_userbot
    await start_userbot()

    yield

    from app.userbot.client import stop_userbot
    await stop_userbot()
    await bot.session.close()
    await engine.dispose()


app = FastAPI(title="TG Bot API", lifespan=lifespan)

from app.api.routes import router as api_router   # noqa: E402
from app.api.auth   import router as auth_router  # noqa: E402
from app.api.admin  import router as admin_router # noqa: E402

app.include_router(api_router,   prefix="/api")
app.include_router(auth_router)
app.include_router(admin_router, prefix="/api")
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def dashboard() -> FileResponse:
    return FileResponse("static/index.html")


@app.post("/webhook")
async def webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str = Header(default=""),
) -> Response:
    if settings.webhook_secret and x_telegram_bot_api_secret_token != settings.webhook_secret:
        raise HTTPException(status_code=403, detail="Invalid secret token")
    body = await request.body()
    update = Update.model_validate_json(body)
    await dp.feed_update(bot, update)
    return Response(status_code=200)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
