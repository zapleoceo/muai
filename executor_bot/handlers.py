import logging

from aiogram import Bot, Router
from aiogram.types import Message

logger = logging.getLogger(__name__)
router = Router()

_executor_id: int | None = None
_known_chats: dict[int, dict] = {}


def set_executor_id(eid: int) -> None:
    global _executor_id
    _executor_id = eid


def get_known_chats() -> list[dict]:
    return list(_known_chats.values())


@router.message()
async def on_message(msg: Message, bot: Bot) -> None:
    if not msg.chat or msg.chat.type == "private":
        return

    _known_chats[msg.chat.id] = {
        "chat_id": msg.chat.id,
        "chat_title": msg.chat.title or str(msg.chat.id),
        "chat_type": msg.chat.type,
        "can_send": True,
    }

    if not _executor_id:
        return

    bot_info = await bot.get_me()

    is_mention = False
    for ent in (msg.entities or []):
        if ent.type == "mention":
            mention_text = msg.text[ent.offset:ent.offset + ent.length] if msg.text else ""
            if mention_text.lstrip("@").lower() == (bot_info.username or "").lower():
                is_mention = True
                break

    is_reply_to_bot = bool(
        msg.reply_to_message
        and msg.reply_to_message.from_user
        and msg.reply_to_message.from_user.id == bot_info.id
    )

    priority = "HIGH" if (is_mention or is_reply_to_bot) else "LOW"

    # Phase 1: only forward HIGH priority
    if priority != "HIGH":
        return

    from executor_bot.config import get_config
    from executor_bot import forwarder
    cfg = get_config()

    payload = {
        "chat_id": msg.chat.id,
        "chat_title": msg.chat.title or str(msg.chat.id),
        "tg_message_id": msg.message_id,
        "from_user_id": msg.from_user.id if msg.from_user else None,
        "from_user_name": (msg.from_user.full_name if msg.from_user else None),
        "text": msg.text or msg.caption or "",
        "is_mention": is_mention,
        "reply_to_msg_id": msg.reply_to_message.message_id if msg.reply_to_message else None,
    }

    try:
        await forwarder.send_inbox(cfg, _executor_id, payload)
        logger.info("Forwarded HIGH priority msg from chat %s", msg.chat.id)
    except Exception as exc:
        logger.warning("Failed to forward to manager: %s", exc)
