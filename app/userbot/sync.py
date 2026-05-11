import logging
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient
from telethon.tl.types import User

from app.db.database import AsyncSessionLocal
from app.db.repository import MessageRepo
from app.userbot.handlers import _chat_title, _chat_type, _media_type

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
    user_cache: dict[int, bool] = {}  # sender_id → already upserted

    try:
        chat = dialog.entity
        chat_id = dialog.id

        async with AsyncSessionLocal() as session:
            repo = MessageRepo(session)
            await repo.upsert_chat_raw(
                id=chat_id,
                type=_chat_type(chat),
                title=_chat_title(chat),
            )
            await session.commit()

        async for msg in client.iter_messages(chat, reverse=True, offset_date=since, limit=None):
            if not msg.text and not msg.media:
                continue

            sender_id = msg.sender_id
            direction = "out" if msg.out else "in"
            dialog_key = f"{chat_id}:{sender_id}" if sender_id else f"{chat_id}"
            date_utc = msg.date.replace(tzinfo=timezone.utc) if msg.date.tzinfo is None else msg.date
            reply_to = msg.reply_to.reply_to_msg_id if msg.reply_to else None

            async with AsyncSessionLocal() as session:
                repo = MessageRepo(session)

                # upsert sender only once per dialog, without extra API calls
                if sender_id and sender_id not in user_cache:
                    sender = msg.sender
                    if sender and isinstance(sender, User):
                        await repo.upsert_user_raw(
                            id=sender.id,
                            username=sender.username,
                            first_name=sender.first_name,
                            last_name=sender.last_name,
                            is_bot=sender.bot,
                        )
                    else:
                        await repo.upsert_user_raw(id=sender_id)
                    user_cache[sender_id] = True

                result = await repo.save_message(
                    chat_id=chat_id,
                    user_id=sender_id,
                    telegram_msg_id=msg.id,
                    direction=direction,
                    text=msg.text or None,
                    media_type=_media_type(msg),
                    caption=msg.message if msg.media and msg.message else None,
                    date_utc=date_utc,
                    reply_to_msg_id=reply_to,
                    dialog_key=dialog_key,
                )
                await session.commit()
                if result:
                    saved += 1
    except Exception:
        logger.exception("Userbot: failed to sync dialog %s", getattr(dialog, "name", dialog.id))
    return saved
