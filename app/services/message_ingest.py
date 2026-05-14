from datetime import datetime, timezone
from io import BytesIO
import logging

from aiogram.types import Message as AiogramMessage
from telethon.tl.types import User as TelethonUser

from app.db.database import AsyncSessionLocal
from app.db.repository import MessageRepo
from app.userbot.media import chat_title, chat_type, chat_username, media_type as telethon_media_type
from app.llm.transcribe import transcribe_audio

logger = logging.getLogger(__name__)


def _aiogram_media_info(msg: AiogramMessage) -> tuple[str | None, str | None]:
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


async def ingest_aiogram_incoming(msg: AiogramMessage) -> bool:
    media_type, file_id = _aiogram_media_info(msg)
    user = msg.from_user
    date_utc = datetime.fromtimestamp(msg.date.timestamp(), tz=timezone.utc) if msg.date else None
    dialog_key = f"{msg.chat.id}:{user.id}" if user else f"{msg.chat.id}"
    text_value = msg.text or msg.caption
    if media_type == "voice" and not text_value and msg.voice and msg.bot:
        try:
            buf = BytesIO()
            f = await msg.bot.get_file(msg.voice.file_id)
            await msg.bot.download_file(f.file_path, destination=buf)
            data = buf.getvalue()
            mime = getattr(msg.voice, "mime_type", None) or "audio/ogg"
            t = await transcribe_audio(data=data, mime_type=mime, language="ru")
            if t:
                text_value = t
        except Exception as exc:
            logger.warning("Voice transcription failed chat=%s msg_id=%s: %s", msg.chat.id, msg.message_id, str(exc)[:200])

    async with AsyncSessionLocal() as session:
        repo = MessageRepo(session)
        await repo.upsert_chat(msg.chat)
        if user:
            await repo.upsert_user(user)
        saved = await repo.save_message(
            chat_id=msg.chat.id,
            user_id=user.id if user else None,
            telegram_msg_id=msg.message_id,
            direction="in",
            text=text_value,
            media_type=media_type,
            file_id=file_id,
            caption=msg.caption if msg.text is None else None,
            raw_json=msg.model_dump(exclude_none=True),
            date_utc=date_utc,
            reply_to_msg_id=msg.reply_to_message.message_id if msg.reply_to_message else None,
            dialog_key=dialog_key,
        )
        await session.commit()
    return saved is not None


async def ingest_aiogram_outgoing(*, chat_id: int, telegram_msg_id: int, text: str, dialog_key: str) -> None:
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


async def ingest_telethon_event(event, *, is_edit: bool = False) -> None:
    msg = event.message
    chat = await event.get_chat()
    sender = await event.get_sender()

    chat_id = event.chat_id
    user_id = sender.id if sender else None
    direction = "out" if msg.out else "in"
    dialog_key = f"{chat_id}:{user_id}" if user_id else f"{chat_id}"
    date_utc = msg.date.replace(tzinfo=timezone.utc) if msg.date.tzinfo is None else msg.date
    reply_to = getattr(msg.reply_to, "reply_to_msg_id", None) if msg.reply_to else None
    edit_date = msg.edit_date.replace(tzinfo=timezone.utc) if is_edit and msg.edit_date else None

    async with AsyncSessionLocal() as session:
        repo = MessageRepo(session)
        await repo.upsert_chat_raw(id=chat_id, type=chat_type(chat), title=chat_title(chat), username=chat_username(chat))
        if sender and isinstance(sender, TelethonUser):
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
            media_type=telethon_media_type(msg),
            caption=msg.message if msg.media and msg.message else None,
            date_utc=date_utc,
            reply_to_msg_id=reply_to,
            edit_date=edit_date,
            dialog_key=dialog_key,
        )
        await session.commit()


async def ingest_telethon_history_message(
    *,
    chat_id: int,
    msg,
    user_cache: dict[int, bool],
) -> bool:
    sender_id = msg.sender_id
    direction = "out" if msg.out else "in"
    dialog_key = f"{chat_id}:{sender_id}" if sender_id else f"{chat_id}"
    date_utc = msg.date.replace(tzinfo=timezone.utc) if msg.date.tzinfo is None else msg.date
    reply_to = getattr(msg.reply_to, "reply_to_msg_id", None) if msg.reply_to else None

    async with AsyncSessionLocal() as session:
        repo = MessageRepo(session)

        if sender_id and sender_id not in user_cache:
            sender = msg.sender
            if sender and isinstance(sender, TelethonUser):
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
            media_type=telethon_media_type(msg),
            caption=msg.message if msg.media and msg.message else None,
            date_utc=date_utc,
            reply_to_msg_id=reply_to,
            dialog_key=dialog_key,
        )
        await session.commit()
    return result is not None
