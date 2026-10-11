"""Реплай владельца на «Открыл задачу …»: «да»/«нет» подтверждает или отклоняет авторство.

Фильтр пускает сюда только реплаи на сообщения, для которых есть ссылка; решает
обработчик: чужой `from.id` и не-приватный чат — молча мимо, ничего не пишется.
"""
from __future__ import annotations

import logging

from aiogram.types import Message
from vera_shared.voice_help.owner_reply import (
    HINT_TEXT,
    NO_REPLY,
    STALE_TEXT,
    USED_TEXT,
    YES_REPLY,
    confirm_by_reply,
    has_link,
)

log = logging.getLogger(__name__)

REPLIES = {"yes": YES_REPLY, "no": NO_REPLY, "used": USED_TEXT, "stale": STALE_TEXT,
           "closed": USED_TEXT, "unknown": HINT_TEXT}


async def is_linked_reply(message: Message) -> bool:
    target = message.reply_to_message
    return target is not None and await has_link(target.message_id)


async def handle_owner_reply(message: Message, owner_id: int) -> str:
    """→ исход (для логов и тестов)."""
    sender = message.from_user
    if (owner_id == 0 or sender is None or sender.id != owner_id
            or message.chat.type != "private" or message.chat.id != owner_id
            or message.reply_to_message is None):
        log.warning("owner-reply: отказ, from=%s chat=%s/%s",
                    sender.id if sender else "?", message.chat.type, message.chat.id)
        return "ignored"
    outcome = await confirm_by_reply(
        chat_id=message.chat.id, link_message_id=message.reply_to_message.message_id,
        reply_message_id=message.message_id, from_id=sender.id, text=message.text)
    if outcome in REPLIES:
        await message.reply(REPLIES[outcome], parse_mode=None)
    return outcome
