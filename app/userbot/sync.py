import logging
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient

from app.db.database import AsyncSessionLocal
from app.db.repository import MessageRepo
from app.userbot.media import chat_title, chat_type
from app.userbot.storage import save_history_message

logger = logging.getLogger(__name__)


async def sync_history(client: TelegramClient, days: int = 2) -> None:
    since = datetime.now(tz=timezone.utc) - timedelta(days=days)
    logger.info("Userbot: syncing history since %s (%d days)", since.date(), days)

    total = 0
    async for dialog in client.iter_dialogs():
        saved = await _sync_dialog(client, dialog, since)
        if saved:
            logger.info("  %s: +%d messages", dialog.name or dialog.id, saved)
        total += saved

    logger.info("Userbot: history sync done, %d messages saved", total)


async def _sync_dialog(client: TelegramClient, dialog, since: datetime) -> int:
    saved = 0
    user_cache: dict[int, bool] = {}

    try:
        chat = dialog.entity
        chat_id = dialog.id

        async with AsyncSessionLocal() as session:
            await MessageRepo(session).upsert_chat_raw(
                id=chat_id,
                type=chat_type(chat),
                title=chat_title(chat),
            )
            await session.commit()

        async for msg in client.iter_messages(chat, reverse=True, offset_date=since, limit=None):
            if not msg.text and not msg.media:
                continue
            if await save_history_message(
                chat_id=chat_id,
                chat_entity=chat,
                msg=msg,
                user_cache=user_cache,
            ):
                saved += 1

    except Exception:
        logger.exception("Userbot: failed to sync dialog %s", getattr(dialog, "name", dialog.id))

    return saved
