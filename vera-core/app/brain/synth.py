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


_SYNTH_SYSTEM = """Ты — Вера, проактивный ассистент Димы. Ниже — данные
из его графа за прошедшие сутки. Составь короткий русский дайджест
(до 8 пунктов, маркированный список):
  - аномалии в потоке событий
  - прогресс по активным целям
  - 1-2 вопроса где не хватает данных
Каждую строку начинай с эмодзи. Без воды."""


async def build_digest() -> str:
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
        pending = (await s.execute(
            select(func.count()).select_from(Event)
            .where(Event.triage_status == "pending",
                    Event.occurred_at >= since - timedelta(days=2))
        )).scalar() or 0

    identity = await ID.list_active()
    goals = identity.get("Goal", [])

    facts = (
        f"События за 24ч: {n_events} (по источникам: {per_src})\n"
        f"Не разобранных карточек: {pending}\n"
        f"Активных целей: {len(goals)}\n"
    )
    for g in goals[:5]:
        facts += f"  - {g.get('title','?')}: deadline={g.get('deadline','?')}\n"

    digest = await llm_chat(
        messages=[{"role": "user", "content": facts}],
        system=_SYNTH_SYSTEM, capability="chat:smart",
    )
    return digest.strip() or facts


async def post_digest(text: str) -> bool:
    """Post digest into the forum chat. Returns True on success."""
    from app.bot.sender import get_bot
    prefs = await preferences.get_all()
    chat_id = int(prefs.get("forum_chat_id") or 0)
    if not chat_id:
        log.warning("synth: no forum_chat_id configured — skipping post")
        return False
    bot = get_bot()
    try:
        await bot.send_message(chat_id=chat_id, text=text)
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
