import logging

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

from app.bot.handlers.messages import _llm_respond
from app.bot.storage import save_incoming

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
    await save_incoming(msg)
    question = (msg.text or "").partition(" ")[2].strip() or None
    await _llm_respond(msg, question=question)
