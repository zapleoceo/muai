"""Telegram bot — Дима пишет, Вера 3.0 отвечает через search service."""
from __future__ import annotations

import asyncio
import logging
import os

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message

log = logging.getLogger(__name__)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OWNER_ID = int(os.environ.get("OWNER_TELEGRAM_ID", "0"))
SEARCH_URL = os.environ.get("SEARCH_URL", "http://brain-search:8000")

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
    from sqlalchemy import select, func
    from vera_shared.db.engine import get_session
    from vera_shared.db.models import EventRow, TokenRow

    async with get_session() as s:
        total_events = (await s.execute(select(func.count(EventRow.id)))).scalar() or 0
        triaged = (await s.execute(
            select(func.count(EventRow.id)).where(EventRow.triage_status == "done")
        )).scalar() or 0
        with_emb = (await s.execute(
            select(func.count(EventRow.id))
            .where(EventRow.embedding_voyage_3.is_not(None))
        )).scalar() or 0
        free_tokens = (await s.execute(
            select(func.count(TokenRow.id))
            .where(TokenRow.is_active.is_(True))
            .where(TokenRow.tier == "free")
        )).scalar() or 0

    pct_triaged = 100 * triaged // max(total_events, 1)
    pct_emb = 100 * with_emb // max(total_events, 1)
    await message.reply(
        f"<b>Vera 3.0 stats</b>\n"
        f"События: <b>{total_events}</b>\n"
        f"Триаж: <b>{triaged}</b> ({pct_triaged}%)\n"
        f"Embeddings: <b>{with_emb}</b> ({pct_emb}%)\n"
        f"Free LLM keys активны: <b>{free_tokens}</b>"
    )


@dp.message(F.text)
async def on_message(message: Message):
    if not _owner_only(message):
        return
    query = message.text or ""
    if not query.strip():
        return

    placeholder = await message.reply("🤔 Думаю…")

    try:
        async with httpx.AsyncClient(timeout=120.0) as c:
            r = await c.post(
                f"{SEARCH_URL}/search",
                json={"q": query, "limit": 15},
            )
        if r.status_code != 200:
            await placeholder.edit_text(f"⚠ Ошибка поиска: HTTP {r.status_code}")
            return
        data = r.json()
        answer = data.get("answer", "(пустой ответ)")
        provider = data.get("provider") or "—"
        cost = data.get("cost_usd", 0.0)
        n_results = len(data.get("results", []))

        footer = f"\n\n<i>via {provider}, ${cost:.4f}, {n_results} событий</i>"
        full = (answer + footer)[:4096]
        await placeholder.edit_text(full)
    except Exception as e:
        log.exception("Reply failed: %s", e)
        await placeholder.edit_text(f"⚠ Ошибка: {e}")


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log.info("Vera 3.0 bot starting, owner=%s", OWNER_ID)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
