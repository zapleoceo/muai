import logging
from datetime import timezone

from telethon import TelegramClient, events
from telethon.tl.types import Channel, Chat, User

from app.db.database import AsyncSessionLocal
from app.db.repository import MessageRepo

logger = logging.getLogger(__name__)


def _chat_type(entity) -> str:
    if isinstance(entity, User):
        return "private"
    if isinstance(entity, Chat):
        return "group"
    if isinstance(entity, Channel):
        return "supergroup" if entity.megagroup else "channel"
    return "unknown"


def _chat_title(entity) -> str | None:
    if isinstance(entity, User):
        return entity.first_name
    return getattr(entity, "title", None)


def _media_type(msg) -> str | None:
    if msg.photo:
        return "photo"
    if msg.voice:
        return "voice"
    if msg.video:
        return "video"
    if msg.document:
        return "document"
    if msg.sticker:
        return "sticker"
    if msg.audio:
        return "audio"
    return None


def register_handlers(client: TelegramClient) -> None:
    @client.on(events.NewMessage)
    async def on_new_message(event) -> None:
        await _save(event)

    @client.on(events.MessageEdited)
    async def on_edited(event) -> None:
        await _save(event, is_edit=True)


async def _save(event, is_edit: bool = False) -> None:
    try:
        msg = event.message
        chat = await event.get_chat()
        sender = await event.get_sender()

        chat_id = event.chat_id
        user_id = sender.id if sender else None
        direction = "out" if msg.out else "in"
        dialog_key = f"{chat_id}:{user_id}" if user_id else f"{chat_id}"
        date_utc = msg.date.replace(tzinfo=timezone.utc) if msg.date.tzinfo is None else msg.date
        reply_to = msg.reply_to.reply_to_msg_id if msg.reply_to else None
        edit_date = msg.edit_date.replace(tzinfo=timezone.utc) if msg.edit_date else None

        async with AsyncSessionLocal() as session:
            repo = MessageRepo(session)
            await repo.upsert_chat_raw(
                id=chat_id,
                type=_chat_type(chat),
                title=_chat_title(chat),
            )
            if sender and isinstance(sender, User):
                await repo.upsert_user_raw(
                    id=sender.id,
                    username=sender.username,
                    first_name=sender.first_name,
                    last_name=sender.last_name,
                    is_bot=sender.bot,
                )
            await repo.save_message(
                chat_id=chat_id,
                user_id=user_id,
                telegram_msg_id=msg.id,
                direction=direction,
                text=msg.text or None,
                media_type=_media_type(msg),
                caption=msg.message if msg.media and msg.message else None,
                date_utc=date_utc,
                reply_to_msg_id=reply_to,
                edit_date=edit_date if is_edit else None,
                dialog_key=dialog_key,
            )
            await session.commit()
    except Exception:
        logger.exception("Userbot: failed to save message")
