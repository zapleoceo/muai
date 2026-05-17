import logging
from collections import deque

from aiogram import Bot, Router
from aiogram.types import Message

logger = logging.getLogger(__name__)
router = Router()

_executor_id: int | None = None
_known_chats: dict[int, dict] = {}
_chat_history: dict[int, deque] = {}  # ring buffer: last N messages per chat

_HISTORY_SIZE = 15
_CONTEXT_SEND = 10  # messages sent to manager as context


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

    text = msg.text or msg.caption or ""
    if text and msg.from_user:
        buf = _chat_history.setdefault(msg.chat.id, deque(maxlen=_HISTORY_SIZE))
        buf.append({
            "msg_id": msg.message_id,
            "from": msg.from_user.full_name,
            "text": text,
            "date": msg.date.isoformat() if msg.date else None,
        })

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

    if not (is_mention or is_reply_to_bot):
        return

    from executor_bot.config import get_config
    from executor_bot import forwarder
    cfg = get_config()

    quoted_text: str | None = None
    quoted_from: str | None = None
    if msg.reply_to_message:
        quoted_text = msg.reply_to_message.text or msg.reply_to_message.caption or None
        if msg.reply_to_message.from_user:
            quoted_from = msg.reply_to_message.from_user.full_name

    history = _chat_history.get(msg.chat.id)
    context_messages: list[dict] = []
    if history:
        prev = [m for m in history if m["msg_id"] != msg.message_id]
        context_messages = [
            {"from": m["from"], "text": m["text"], "date": m["date"]}
            for m in prev[-_CONTEXT_SEND:]
        ]

    payload = {
        "chat_id": msg.chat.id,
        "chat_title": msg.chat.title or str(msg.chat.id),
        "tg_message_id": msg.message_id,
        "from_user_id": msg.from_user.id if msg.from_user else None,
        "from_user_name": msg.from_user.full_name if msg.from_user else None,
        "text": text,
        "is_mention": is_mention,
        "reply_to_msg_id": msg.reply_to_message.message_id if msg.reply_to_message else None,
        "quoted_text": quoted_text,
        "quoted_from": quoted_from,
        "context_messages": context_messages or None,
    }

    try:
        await forwarder.send_inbox(cfg, _executor_id, payload)
        logger.info("Forwarded HIGH priority msg from chat %s (mention=%s reply=%s)", msg.chat.id, is_mention, is_reply_to_bot)
    except Exception as exc:
        logger.warning("Failed to forward to manager: %s", exc)
