from aiogram import Bot
from aiogram.types import Message

from app.config import get_settings

_bot: Bot | None = None


def init_bot(bot: Bot) -> None:
    global _bot
    _bot = bot


def get_bot() -> Bot:
    if _bot is None:
        raise RuntimeError("Bot not initialised — call init_bot() first")
    return _bot


async def send_to_group(text: str, parse_mode: str = "HTML") -> None:
    settings = get_settings()
    await get_bot().send_message(
        chat_id=settings.vera_group_id,
        text=text,
        parse_mode=parse_mode,
    )


async def reply(message: Message, text: str) -> None:
    await message.reply(text)


async def notify_group(text: str) -> None:
    await send_to_group(text)
