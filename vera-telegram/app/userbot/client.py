import logging

from telethon import TelegramClient

from app.config import get_settings
from app.userbot.session import get_session_path

logger = logging.getLogger(__name__)

_client: TelegramClient | None = None


def get_client() -> TelegramClient:
    global _client
    cfg = get_settings()
    if _client is None:
        session = get_session_path()
        # Strip .session suffix — Telethon appends it automatically
        if session.endswith(".session"):
            session = session[: -len(".session")]
        _client = TelegramClient(session, cfg.telegram_api_id, cfg.telegram_api_hash)
    return _client


async def start_client() -> None:
    client = get_client()
    # connect=True but no interactive auth — session file must already exist
    await client.connect()
    if not await client.is_user_authorized():
        logger.error("Telethon session is not authorized. Run auth_userbot.py first.")
        return
    me = await client.get_me()
    logger.info("Telethon userbot connected as %s (id=%s)", me.first_name, me.id)


async def stop_client() -> None:
    if _client is not None and _client.is_connected():
        await _client.disconnect()
        logger.info("Telethon client disconnected")
