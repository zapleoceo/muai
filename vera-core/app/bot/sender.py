from aiogram import Bot
from aiogram.types import Message

from app.config import get_settings

_bot: Bot | None = None
_MAX_LEN = 4000


def init_bot(bot: Bot) -> None:
    global _bot
    _bot = bot


def get_bot() -> Bot:
    if _bot is None:
        raise RuntimeError("Bot not initialised — call init_bot() first")
    return _bot


def _chunks(text: str, size: int = _MAX_LEN) -> list[str]:
    if len(text) <= size:
        return [text]
    out: list[str] = []
    while text:
        if len(text) <= size:
            out.append(text)
            break
        cut = text.rfind("\n", 0, size)
        if cut < size // 2:
            cut = size
        out.append(text[:cut])
        text = text[cut:].lstrip()
    return out


async def send_to_group(text: str, parse_mode: str = "HTML") -> None:
    settings = get_settings()
    bot = get_bot()
    for part in _chunks(text):
        await bot.send_message(chat_id=settings.vera_group_id, text=part, parse_mode=parse_mode)


async def reply(message: Message, text: str) -> None:
    parts = _chunks(text)
    await message.reply(parts[0])
    for part in parts[1:]:
        await message.answer(part)


async def notify_group(text: str) -> None:
    await send_to_group(text)
