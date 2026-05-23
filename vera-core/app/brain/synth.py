"""Daily proactive synthesis — Vera initiates, doesn't wait.

Once a day Vera reads the graph and posts a digest into the forum
chat with:
  - anomalies vs recent patterns (new senders, unusual times, spikes)
  - goal progress (% toward each active Goal)
  - pending decisions still waiting on Dima
  - suggestions she wants to learn

Implementation:
  - synth_loop() runs in background, fires daily at 08:00 local.
  - build_digest() composes the text from graph queries + LLM summary.
  - post_digest() sends it to forum_chat_id with [✓ Принято] [✏ Поправь]
    inline buttons. Reactions feed back into Pattern/Value updates.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta, timezone

from sqlalchemy import desc, func, select

from vera_shared.db.engine import get_session
from vera_shared.db.models import Event
from vera_shared.llm.router import chat as llm_chat

from app.bot import preferences
from app.brain import identity as ID

log = logging.getLogger(__name__)

_DAILY_HOUR = 8  # local time, Asia/Bangkok offset handled by Telegram


_SYNTH_SYSTEM = """Ты — Вера, второе я Димы. Ниже — данные из его графа
за сутки. Составь компактный русский дайджест в формате:

🌅 *Доброе утро* (одна строка с твоим стилем)

📊 *Итоги суток:*
• Общая статистика (1-2 строки)

👥 *Топ-контакты:* (если есть)
• Имя — N сообщений, контекст

💬 *Главные диалоги:*
• Чат/тема — N событий, о чём

⚠️ *Требует решения:*
• #ID — короткое описание (если есть awaiting_user)

🎯 *Цели и дедлайны:*
• Прогресс по активным целям

❓ *Вопросы/наблюдения:*
• 1-2 пункта где не хватает данных или замечена аномалия

Стиль: краткий, дружеский, по делу. Без эмодзи кроме заголовков
разделов. Без воды. Если данных по разделу нет — пропусти раздел."""


async def build_digest() -> str:
    """Build a grouped daily digest.

    Groups events by sender/chat (top noisy talkers), highlights:
      - actionable (status=awaiting_user, silenced with high score)
      - top conversations (by event count)
      - active goals with deadline pressure
    Asks LLM to render a compact markdown bullet list.
    """
    since = datetime.utcnow() - timedelta(hours=24)
    async with get_session() as s:
        n_events = (await s.execute(
            select(func.count()).select_from(Event)
            .where(Event.occurred_at >= since)
        )).scalar() or 0
        per_src = dict((await s.execute(
            select(Event.source, func.count()).where(Event.occurred_at >= since)
            .group_by(Event.source)
        )).all())
        per_status = dict((await s.execute(
            select(Event.triage_status, func.count())
            .where(Event.occurred_at >= since)
            .group_by(Event.triage_status)
        )).all())
        awaiting = (await s.execute(
            select(Event).where(Event.triage_status == "awaiting_user",
                                  Event.occurred_at >= since - timedelta(days=2))
            .order_by(Event.id.desc()).limit(10)
        )).scalars().all()
        # Recent events for grouping (last 24h)
        recent = (await s.execute(
            select(Event).where(Event.occurred_at >= since)
            .order_by(Event.id.desc()).limit(300)
        )).scalars().all()

    # Group by first person hint
    by_person: dict[str, int] = {}
    by_chat: dict[str, int] = {}
    for ev in recent:
        hints = ev.entity_hints or []
        for h in hints:
            if h.get("type") == "person":
                pid = h.get("name") or h.get("identifier") or "?"
                by_person[pid] = by_person.get(pid, 0) + 1
                break
        for h in hints:
            if h.get("type") in ("chat", "topic", "folder"):
                cid = h.get("name") or h.get("identifier") or "?"
                by_chat[cid] = by_chat.get(cid, 0) + 1
                break
    top_people = sorted(by_person.items(), key=lambda x: -x[1])[:8]
    top_chats = sorted(by_chat.items(), key=lambda x: -x[1])[:5]

    identity = await ID.list_active()
    goals = identity.get("Goal", [])

    facts = [
        f"СУТОЧНАЯ СВОДКА для дайджеста.",
        f"События: {n_events} (источники: {per_src}, статусы: {per_status}).",
        f"Активных целей: {len(goals)}.",
    ]
    if goals:
        facts.append("Цели:")
        for g in goals[:5]:
            facts.append(f"  - {g.get('title','?')} (deadline: {g.get('deadline','?')})")
    if top_people:
        facts.append("Топ собеседники по числу сообщений:")
        for name, n in top_people:
            facts.append(f"  - {name}: {n}")
    if top_chats:
        facts.append("Топ чаты/потоки:")
        for name, n in top_chats:
            facts.append(f"  - {name}: {n}")
    if awaiting:
        facts.append(f"Карточки ждут решения ({len(awaiting)}):")
        for ev in awaiting[:5]:
            preview = (ev.content_text or "")[:80].replace("\n", " ")
            facts.append(f"  - #{ev.id} {ev.source}: {preview}")

    facts_text = "\n".join(facts)
    digest = await llm_chat(
        messages=[{"role": "user", "content": facts_text}],
        system=_SYNTH_SYSTEM, capability="chat:smart",
    )
    return digest.strip() or facts_text


async def post_digest(text: str) -> bool:
    """Post digest into the forum chat with feedback buttons."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    from app.bot.sender import get_bot
    prefs = await preferences.get_all()
    chat_id = int(prefs.get("forum_chat_id") or 0)
    if not chat_id:
        log.warning("synth: no forum_chat_id configured — skipping post")
        return False
    bot = get_bot()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="👍 норм", callback_data="dig:ok"),
        InlineKeyboardButton(text="🔍 подробнее", callback_data="dig:more"),
        InlineKeyboardButton(text="🤫 тише", callback_data="dig:quiet"),
        InlineKeyboardButton(text="📢 громче", callback_data="dig:loud"),
    ]])
    try:
        await bot.send_message(chat_id=chat_id, text=text,
                                parse_mode="Markdown", reply_markup=kb)
        return True
    except Exception as exc:
        log.exception("synth: post failed: %s", exc)
        return False


async def synth_loop() -> None:
    """Fire once per day at _DAILY_HOUR local. Sleeps remainder."""
    while True:
        try:
            now = datetime.now(timezone.utc)
            target = now.replace(hour=_DAILY_HOUR, minute=0, second=0,
                                  microsecond=0)
            if target <= now:
                target = target + timedelta(days=1)
            wait_s = max(60.0, (target - now).total_seconds())
            log.info("synth_loop: sleeping %.0fs until next digest", wait_s)
            await asyncio.sleep(wait_s)
            text = await build_digest()
            await post_digest(text)
        except asyncio.CancelledError:
            log.info("synth_loop cancelled")
            raise
        except Exception as exc:
            log.exception("synth_loop iteration failed: %s", exc)
            await asyncio.sleep(3600)


def start() -> None:
    from app.common.bg import spawn
    spawn(synth_loop(), name="synth_loop")
    log.info("brain.synth: daily digest loop spawned")
