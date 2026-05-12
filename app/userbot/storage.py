from datetime import timezone

from telethon.tl.types import User

from app.db.database import AsyncSessionLocal
from app.db.repository import MessageRepo
from app.userbot.media import chat_title, chat_type, chat_username, media_type


async def save_event(event, *, is_edit: bool = False) -> None:
    msg = event.message
    chat = await event.get_chat()
    sender = await event.get_sender()

    chat_id = event.chat_id
    user_id = sender.id if sender else None
    direction = "out" if msg.out else "in"
    dialog_key = f"{chat_id}:{user_id}" if user_id else f"{chat_id}"
    date_utc = msg.date.replace(tzinfo=timezone.utc) if msg.date.tzinfo is None else msg.date
    reply_to = getattr(msg.reply_to, 'reply_to_msg_id', None) if msg.reply_to else None
    edit_date = msg.edit_date.replace(tzinfo=timezone.utc) if is_edit and msg.edit_date else None

    async with AsyncSessionLocal() as session:
        repo = MessageRepo(session)
        await repo.upsert_chat_raw(id=chat_id, type=chat_type(chat), title=chat_title(chat), username=chat_username(chat))
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
            media_type=media_type(msg),
            caption=msg.message if msg.media and msg.message else None,
            date_utc=date_utc,
            reply_to_msg_id=reply_to,
            edit_date=edit_date,
            dialog_key=dialog_key,
        )
        await session.commit()


async def save_history_message(
    *,
    chat_id: int,
    chat_entity,
    msg,
    user_cache: dict[int, bool],
) -> bool:
    """Persist one historical message. Returns True if newly saved."""
    sender_id = msg.sender_id
    direction = "out" if msg.out else "in"
    dialog_key = f"{chat_id}:{sender_id}" if sender_id else f"{chat_id}"
    date_utc = msg.date.replace(tzinfo=timezone.utc) if msg.date.tzinfo is None else msg.date
    reply_to = getattr(msg.reply_to, 'reply_to_msg_id', None) if msg.reply_to else None

    async with AsyncSessionLocal() as session:
        repo = MessageRepo(session)

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
            media_type=media_type(msg),
            caption=msg.message if msg.media and msg.message else None,
            date_utc=date_utc,
            reply_to_msg_id=reply_to,
            dialog_key=dialog_key,
        )
        await session.commit()
    return result is not None
