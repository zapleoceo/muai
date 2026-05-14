import logging
from zoneinfo import ZoneInfo

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

from app.bot.handlers.messages import _llm_respond
from app.services.message_ingest import ingest_aiogram_incoming
from app.services.timezone import get_user_timezone, set_user_timezone

logger = logging.getLogger(__name__)
router = Router()


@router.message(Command("start"))
async def cmd_start(msg: Message) -> None:
    await msg.answer(
        "Привет! Я бот-ассистент.\n\n"
        "• Просто напиши мне — отвечу на основе истории чата.\n"
        "• <b>/ai</b> [вопрос] — принудительный вызов в группах."
    )


@router.message(Command("ai"))
async def cmd_ai(msg: Message, bot: Bot) -> None:
    inserted = await ingest_aiogram_incoming(msg)
    if not inserted:
        return
    question = (msg.text or "").partition(" ")[2].strip() or None
    await _llm_respond(msg, question=question)


@router.message(Command("timezone"))
async def cmd_timezone(msg: Message) -> None:
    user = msg.from_user
    if not user:
        return
    arg = (msg.text or "").partition(" ")[2].strip()
    if not arg:
        cur = await get_user_timezone(user.id)
        await msg.answer(f"Текущий timezone: <code>{cur}</code>\nПример установки: <code>/timezone Europe/Moscow</code>")
        return
    try:
        ZoneInfo(arg)
    except Exception:
        await msg.answer("Неверный timezone. Пример: <code>/timezone Europe/Moscow</code>")
        return
    await set_user_timezone(user.id, arg)
    await msg.answer(f"Timezone сохранён: <code>{arg}</code>")
