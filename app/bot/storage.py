from datetime import datetime, timezone

from aiogram.types import Message

from app.db.database import AsyncSessionLocal
from app.db.repository import MessageRepo
from app.llm.base import LLMMessage


def _media_info(msg: Message) -> tuple[str | None, str | None]:
    """Return (media_type, file_id) for the first media attachment."""
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


async def save_incoming(msg: Message) -> None:
    media_type, file_id = _media_info(msg)
    user = msg.from_user
    date_utc = datetime.fromtimestamp(msg.date.timestamp(), tz=timezone.utc) if msg.date else None
    dialog_key = f"{msg.chat.id}:{user.id}" if user else f"{msg.chat.id}"

    async with AsyncSessionLocal() as session:
        repo = MessageRepo(session)
        await repo.upsert_chat(msg.chat)
        if user:
            await repo.upsert_user(user)
        await repo.save_message(
            chat_id=msg.chat.id,
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


async def save_outgoing(
    *,
    chat_id: int,
    telegram_msg_id: int,
    text: str,
    dialog_key: str,
) -> None:
    async with AsyncSessionLocal() as session:
        repo = MessageRepo(session)
        await repo.save_message(
            chat_id=chat_id,
            user_id=None,
            telegram_msg_id=telegram_msg_id,
            direction="out",
            text=text,
            date_utc=datetime.now(tz=timezone.utc),
            dialog_key=dialog_key,
        )
        await session.commit()


async def get_dialog_context(chat_id: int, limit: int = 20) -> list[LLMMessage]:
    async with AsyncSessionLocal() as session:
        rows = await MessageRepo(session).get_messages(chat_id=chat_id, limit=limit)
    return [
        LLMMessage(
            role="assistant" if r.direction == "out" else "user",
            content=r.text or r.caption or f"[{r.media_type or 'media'}]",
        )
        for r in rows
    ]
