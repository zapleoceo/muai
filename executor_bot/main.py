import asyncio
import logging

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from executor_bot.config import get_config
from executor_bot import forwarder, handlers
from executor_bot.sender import make_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)


async def main() -> None:
    cfg = get_config()
    bot = Bot(token=cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(handlers.router)

    app = make_app(bot, cfg.executor_api_secret)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", cfg.executor_api_port)
    await site.start()
    logger.info("Executor API server started on port %d", cfg.executor_api_port)

    bot_info = await bot.get_me()
    for attempt in range(5):
        try:
            eid = await forwarder.register(cfg, bot_info.username or "", handlers.get_known_chats())
            handlers.set_executor_id(eid)
            logger.info("Registered with Manager as executor_id=%d", eid)
            break
        except Exception as exc:
            logger.warning("Registration attempt %d failed: %s", attempt + 1, exc)
            await asyncio.sleep(5)

    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
