import asyncio
import logging
from contextlib import asynccontextmanager

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

log = logging.getLogger(__name__)


class ProgressIndicator:
    def __init__(self, bot: Bot, message: Message) -> None:
        self._bot = bot
        self._message = message
        self._placeholder: Message | None = None
        self._last_text = ""
        self._typing_task: asyncio.Task | None = None

    async def start(self, text: str) -> None:
        self._placeholder = await self._message.reply(text)
        self._last_text = text
        self._typing_task = asyncio.create_task(self._keep_typing())

    async def update(self, text: str) -> None:
        if self._placeholder is None or text == self._last_text:
            return
        try:
            await self._placeholder.edit_text(text)
            self._last_text = text
        except TelegramBadRequest:
            pass

    async def finish(self, text: str) -> None:
        await self._stop_typing()
        if self._placeholder is None:
            await self._message.reply(text)
            return
        try:
            await self._placeholder.edit_text(text)
        except TelegramBadRequest as exc:
            if "message is too long" in str(exc).lower() or "can't be edited" in str(exc).lower():
                try:
                    await self._placeholder.delete()
                except Exception:
                    pass
                await self._message.reply(text[:4000])
                for chunk in _split_after(text[4000:], 4000):
                    await self._message.answer(chunk)
            else:
                log.warning("edit_text failed: %s", exc)

    async def _keep_typing(self) -> None:
        try:
            while True:
                await self._bot.send_chat_action(self._message.chat.id, "typing")
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _stop_typing(self) -> None:
        if self._typing_task and not self._typing_task.done():
            self._typing_task.cancel()
            try:
                await self._typing_task
            except Exception:
                pass


def _split_after(text: str, size: int) -> list[str]:
    out = []
    while text:
        if len(text) <= size:
            out.append(text)
            break
        cut = text.rfind("\n", 0, size) or size
        out.append(text[:cut])
        text = text[cut:].lstrip()
    return out


@asynccontextmanager
async def progress(bot: Bot, message: Message, initial: str = "🤔 Думаю..."):
    p = ProgressIndicator(bot, message)
    await p.start(initial)
    try:
        yield p
    except Exception:
        await p._stop_typing()
        raise
