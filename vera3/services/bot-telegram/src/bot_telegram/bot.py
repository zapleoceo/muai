"""Telegram bot — Дима пишет, Вера 3.0 отвечает через search service."""
from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import Message

from bot_telegram import voice_worker
from bot_telegram.brain import BrainError, ask_brain, save_event
from bot_telegram.formatting import format_error, format_reply, plain_fallback

log = logging.getLogger(__name__)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OWNER_ID = int(os.environ.get("OWNER_TELEGRAM_ID", "0"))

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()


def _owner_only(message: Message) -> bool:
    # Fail-closed: без OWNER_TELEGRAM_ID бот молчит для всех, а не отвечает всем
    if OWNER_ID == 0:
        log.error("OWNER_TELEGRAM_ID not set — ignoring message from %s",
                  message.from_user.id if message.from_user else "?")
        return False
    return message.from_user is not None and message.from_user.id == OWNER_ID


@dp.message(Command("start", "help"))
async def cmd_start(message: Message):
    if not _owner_only(message):
        return
    await message.reply(
        "Привет. Я Вера 3.0 — твоя цифровая память.\n\n"
        "Просто напиши вопрос — я найду ответ в твоей истории "
        "(письма, чаты, события за всё время что я записываю).\n\n"
        "Команды:\n"
        "/stats — статистика мозга\n"
        "/help — это сообщение"
    )


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if not _owner_only(message):
        return
    from sqlalchemy import func, select, text
    from vera_shared.db.engine import get_session
    from vera_shared.db.models import EventRow

    async with get_session() as s:
        total_events = (await s.execute(select(func.count(EventRow.id)))).scalar() or 0
        triaged = (await s.execute(
            select(func.count(EventRow.id)).where(EventRow.triage_status == "done")
        )).scalar() or 0
        # Эмбеддинги вынесены в event_embeddings (миграция 011)
        with_emb = (await s.execute(
            text("SELECT COUNT(*) FROM event_embeddings"))).scalar() or 0

    pct_triaged = 100 * triaged // max(total_events, 1)
    pct_emb = 100 * with_emb // max(total_events, 1)
    await message.reply(
        f"<b>Vera 3.0 stats</b>\n"
        f"События: <b>{total_events}</b>\n"
        f"Триаж: <b>{triaged}</b> ({pct_triaged}%)\n"
        f"Embeddings: <b>{with_emb}</b> ({pct_emb}%)\n"
        f"LLM: через брокер (aib.zapleo.com)"
    )


@dp.message(F.text)
async def on_message(message: Message):
    if not _owner_only(message):
        return
    query = message.text or ""
    if not query.strip():
        return

    chat_id = message.chat.id
    user_id = message.from_user.id

    # Сохраняем вопрос Димы как событие (попадёт в триаж/embed/search)
    await save_event(chat_id, message.message_id, "user", query,
                     sender_id=user_id, occurred_at=message.date)

    placeholder = await message.reply("🤔 Думаю…")

    try:
        try:
            answer = await ask_brain(query, chat_id, user_id)
        except BrainError as e:
            await placeholder.edit_text(f"⚠ Ошибка поиска: {e}")
            return
        raw_answer, provider = answer.raw, answer.provider
        reply_text = format_reply(raw_answer, provider, answer.cost_usd,
                                  answer.n_results, answer.n_history)
        try:
            sent = await placeholder.edit_text(reply_text)
        except TelegramBadRequest as e:
            # Экранирование в format_reply должно исключать это, но это
            # второй рубеж: если Telegram всё равно отклонил HTML (edge
            # case юникода/лимитов) — шлём без всякого форматирования,
            # чтобы Дима гарантированно получил ответ, а не тишину.
            log.warning("HTML reply rejected by Telegram (%s) — plain fallback", e)
            sent = await placeholder.edit_text(
                plain_fallback(raw_answer, provider), parse_mode=None,
            )

        # Сохраняем ответ Веры тоже как событие (сырой текст, без escape)
        reply_msg_id = sent.message_id if hasattr(sent, "message_id") else placeholder.message_id
        await save_event(chat_id, reply_msg_id, "vera", raw_answer)
    except Exception as e:
        log.exception("Reply failed: %s", e)
        try:
            await placeholder.edit_text(format_error(e), parse_mode=None)
        except Exception:
            log.exception("Failed to deliver error message to Telegram")


async def send_to_owner(html: str, plain: str) -> int:
    """Единственный путь, которым бот пишет первым, — и только владельцу."""
    if OWNER_ID == 0:
        raise RuntimeError("OWNER_TELEGRAM_ID not set — refusing to send")
    try:
        sent = await bot.send_message(OWNER_ID, html)
    except TelegramBadRequest as e:
        log.warning("HTML message rejected by Telegram (%s) — plain fallback", e)
        sent = await bot.send_message(OWNER_ID, plain, parse_mode=None)
    return sent.message_id


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log.info("Vera 3.0 bot starting, owner=%s", OWNER_ID)
    from vera_shared.db.engine import init_engine
    await init_engine()
    worker = asyncio.create_task(voice_worker.run_forever(send_to_owner, OWNER_ID),
                                 name="voice-worker")
    try:
        await dp.start_polling(bot)
    finally:
        worker.cancel()


if __name__ == "__main__":
    asyncio.run(main())
