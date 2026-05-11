import logging

from aiogram import Bot, Router
from aiogram.types import Message

from app.bot.storage import save_incoming

logger = logging.getLogger(__name__)
router = Router()


@router.message()
async def handle_message(msg: Message, bot: Bot) -> None:
    try:
        await save_incoming(msg)
    except Exception:
        logger.exception("Failed to save message chat_id=%s msg_id=%s", msg.chat.id, msg.message_id)
