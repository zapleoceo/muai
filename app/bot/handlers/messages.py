import logging
from datetime import datetime, timezone

from aiogram import Bot, Router
from aiogram.types import Message

from app.db.database import AsyncSessionLocal
from app.db.repository import MessageRepo

logger = logging.getLogger(__name__)
router = Router()


def _detect_media(msg: Message) -> tuple[str | None, str | None]:
    """Return (media_type, file_id) for the first media found in a message."""
    if msg.photo:
        return "photo", msg.photo[-1].file_id
    if msg.voice:
        return "voice", msg.voice.file_id
    if msg.video:
        return "video", msg.video.file_id
    if msg.document:
        return "document", msg.document.file_id
    if msg.sticker:
        return "sticker", msg.sticker.file_id
    if msg.audio:
        return "audio", msg.audio.file_id
    return None, None


async def _save_incoming(msg: Message) -> None:
    media_type, file_id = _detect_media(msg)
    chat = msg.chat
    user = msg.from_user

    date_utc = datetime.fromtimestamp(msg.date.timestamp(), tz=timezone.utc) if msg.date else None
    dialog_key = f"{chat.id}:{user.id}" if user else f"{chat.id}"

    async with AsyncSessionLocal() as session:
        repo = MessageRepo(session)
        await repo.upsert_chat(chat)
        if user:
            await repo.upsert_user(user)
        await repo.save_message(
            chat_id=chat.id,
            user_id=user.id if user else None,
            telegram_msg_id=msg.message_id,
            direction="in",
            text=msg.text or msg.caption,
            media_type=media_type,
            file_id=file_id,
            caption=msg.caption if msg.text is None else None,
            raw_json=msg.model_dump(exclude_none=True),
            date_utc=date_utc,
            reply_to_msg_id=msg.reply_to_message.message_id if msg.reply_to_message else None,
            dialog_key=dialog_key,
        )
        await session.commit()


@router.message()
async def handle_message(msg: Message, bot: Bot) -> None:
    try:
        await _save_incoming(msg)
    except Exception:
        logger.exception("Failed to save message chat_id=%s msg_id=%s", msg.chat.id, msg.message_id)
