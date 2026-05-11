import logging

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

from app.bot.storage import save_outgoing
from app.config import get_settings
from app.logic.reply import run_ai_reply

logger = logging.getLogger(__name__)
router = Router()
settings = get_settings()


@router.message(Command("start"))
async def cmd_start(msg: Message) -> None:
    await msg.answer(
        "Привет! Я бот-ассистент.\n\n"
        "• <b>/ai</b> — задать вопрос (берёт последние 20 сообщений как контекст)\n"
        "• Все сообщения сохраняются в базу данных."
    )


@router.message(Command("ai"))
async def cmd_ai(msg: Message, bot: Bot) -> None:
    thinking = await msg.answer("⏳ Думаю...")
    try:
        reply_text = await run_ai_reply(chat_id=msg.chat.id)
    except Exception:
        logger.exception("LLM error for chat_id=%s", msg.chat.id)
        await thinking.edit_text("❌ Ошибка при обращении к LLM.")
        return

    await thinking.edit_text(reply_text)

    dialog_key = f"{msg.chat.id}:{msg.from_user.id}" if msg.from_user else f"{msg.chat.id}"
    await save_outgoing(
        chat_id=msg.chat.id,
        telegram_msg_id=thinking.message_id,
        text=reply_text,
        dialog_key=dialog_key,
    )
