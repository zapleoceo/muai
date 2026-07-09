"""Telegram bot — Дима пишет, Вера 3.0 отвечает через search service."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import Message

from bot_telegram.formatting import format_error, format_reply, plain_fallback

log = logging.getLogger(__name__)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OWNER_ID = int(os.environ.get("OWNER_TELEGRAM_ID", "0"))
SEARCH_URL = os.environ.get("SEARCH_URL", "http://brain-search:8000")
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://gateway:8000")
INTERNAL_SECRET = os.environ.get("INTERNAL_SECRET", "")


async def _save_event(chat_id: int, msg_id: int, role: str, content: str,
                       sender_id: int | None = None, occurred_at: datetime | None = None) -> None:
    """Записать реплику разговора в events table через gateway.

    role: 'user' (Dima) или 'vera' (bot's answer).
    """
    payload = {
        "source": "vera_chat",
        "source_event_id": f"tg:{chat_id}:{msg_id}:{role}",
        "account": f"chat:{chat_id}",
        "category": role,
        "content_text": content[:8000],
        "occurred_at": (occurred_at or datetime.utcnow()).isoformat(),
        "metadata": {
            "chat_id": chat_id,
            "sender_id": sender_id,
            "role": role,
            "msg_id": msg_id,
        },
    }
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"{GATEWAY_URL}/event/vera_chat",
                json=payload,
                headers={"X-Internal-Secret": INTERNAL_SECRET} if INTERNAL_SECRET else {},
            )
        if r.status_code not in (200, 201):
            log.warning("save_event %s: HTTP %s %s", role, r.status_code, r.text[:200])
    except Exception as e:
        log.warning("save_event %s failed: %s", role, e)

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()


def _owner_only(message: Message) -> bool:
    return OWNER_ID == 0 or message.from_user.id == OWNER_ID


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
    await _save_event(chat_id, message.message_id, "user", query,
                       sender_id=user_id, occurred_at=message.date)

    placeholder = await message.reply("🤔 Думаю…")

    try:
        async with httpx.AsyncClient(timeout=120.0) as c:
            r = await c.post(
                f"{SEARCH_URL}/search",
                json={
                    "q": query,
                    "limit": 15,
                    "conversation": {"chat_id": chat_id, "user_id": user_id},
                },
            )
        if r.status_code != 200:
            await placeholder.edit_text(f"⚠ Ошибка поиска: HTTP {r.status_code}")
            return
        data = r.json()
        raw_answer = data.get("answer", "(пустой ответ)")
        provider = data.get("provider") or "—"
        cost = data.get("cost_usd", 0.0)
        n_results = len(data.get("results", []))
        n_history = data.get("history_used", 0)

        reply_text = format_reply(raw_answer, provider, cost, n_results, n_history)
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
        await _save_event(chat_id, reply_msg_id, "vera", raw_answer)
    except Exception as e:
        log.exception("Reply failed: %s", e)
        try:
            await placeholder.edit_text(format_error(e), parse_mode=None)
        except Exception:
            log.exception("Failed to deliver error message to Telegram")


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log.info("Vera 3.0 bot starting, owner=%s", OWNER_ID)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
