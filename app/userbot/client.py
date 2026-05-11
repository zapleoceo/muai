import logging
import os

from telethon import TelegramClient

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

SESSION_PATH = "/app/sessions/userbot"
_client: TelegramClient | None = None


def get_client() -> TelegramClient:
    global _client
    if _client is None:
        _client = TelegramClient(SESSION_PATH, settings.telegram_api_id, settings.telegram_api_hash)
    return _client


async def start_userbot() -> bool:
    if not settings.telegram_api_id or not settings.telegram_api_hash:
        logger.warning("Userbot: TELEGRAM_API_ID / TELEGRAM_API_HASH not set, skipping")
        return False

    if not os.path.exists(f"{SESSION_PATH}.session"):
        logger.warning(
            "Userbot session not found. Run auth first:\n"
            "  docker compose run -it --rm bot python scripts/auth_userbot.py"
        )
        return False

    from app.userbot.handlers import register_handlers
    from app.userbot.sync import sync_history

    client = get_client()
    register_handlers(client)
    await client.start()

    me = await client.get_me()
    logger.info("Userbot started as: %s (id=%s)", me.first_name, me.id)

    if settings.sync_history_days > 0:
        await sync_history(client, days=settings.sync_history_days)

    return True


async def stop_userbot() -> None:
    client = get_client()
    if client.is_connected():
        await client.disconnect()
        logger.info("Userbot disconnected")
