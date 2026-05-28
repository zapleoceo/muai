"""Reactive layer: when an event lands, sniff if it matches a known
Pattern → prepare a draft → DM Дима «вот черновик, отправить?».

Triggered from app/brain/ingest.py at the end of ingest(). Best-effort,
never blocks ingest path. Only fires when ALL of:
  - Pattern exists for the (chat, stem)-signature OR similar trigger
  - confirmation_count >= 3 (or observation_count >= 5 from miner)
  - tool is reversible / safe (defined by AUTO_SAFE_TOOLS in scoring)
  - Last proactive DM for this signature was > 30min ago (anti-spam)

User clicks ✓ → bot sends, writes Pattern confirmation.
User clicks ✗ → Pattern correction, no send.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import get_settings

log = logging.getLogger(__name__)

_last_dm_at: dict[str, datetime] = {}     # signature → last DM time
_DM_COOLDOWN = timedelta(minutes=30)


async def maybe_propose(event_id: int) -> None:
    """Called after each new event lands in the brain. Best-effort."""
    if os.environ.get("VERA_PROACTIVE", "1") != "1":
        return
    try:
        from sqlalchemy import select
        from vera_shared.db.engine import get_session
        from vera_shared.db.models import Event
        from app.brain import patterns as P
        from app.brain.pattern_miner import _view, _stem, _signature

        async with get_session() as s:
            ev = (await s.execute(
                select(Event).where(Event.id == event_id)
            )).scalar_one_or_none()
        if ev is None:
            return
        view = _view(ev)
        # Only react on INCOMING events — proactive about what TO write.
        if view["direction"] == "sent":
            return

        # Sniff: does a sent-template exist for THIS chat that was triggered
        # by similar incoming text before?
        # MVP: just look for any Pattern in this chat with observation>=N.
        from app.graph.client import get_graphiti
        client = await get_graphiti()
        db = get_settings().neo4j_database
        async with client.driver.session(database=db) as ses:
            r = await ses.run(
                "MATCH (p:Pattern) WHERE p.args_template CONTAINS $chat "
                "AND coalesce(p.observation_count, 0) >= 3 "
                "RETURN p.id AS sig, p.action_label AS label, "
                "p.tool AS tool, p.args_template AS args, "
                "p.observation_count AS obs, "
                "p.correction_count AS corr ORDER BY obs DESC LIMIT 1",
                chat=view["chat"],
            )
            row = await r.single()
        if row is None:
            return
        sig = row["sig"]
        if (row.get("corr") or 0) >= row.get("obs", 0) / 2:
            return  # too many user corrections, not worth proposing

        # Anti-spam cooldown
        last = _last_dm_at.get(sig)
        if last and datetime.utcnow() - last < _DM_COOLDOWN:
            return
        _last_dm_at[sig] = datetime.utcnow()

        await _send_proactive_dm(
            sig=sig,
            label=row["label"] or "ответить",
            tool=row["tool"] or "telegram_send_message",
            args_text=row["args"] or "",
            trigger_text=view["text"][:200],
            chat=view["chat"],
            obs=row.get("obs", 0),
        )
    except Exception as exc:
        log.debug("proactive: skip event %s: %s", event_id, exc)


async def _send_proactive_dm(*, sig: str, label: str, tool: str,
                                args_text: str, trigger_text: str,
                                chat: str, obs: int) -> None:
    from app.bot.sender import get_bot
    settings = get_settings()
    if not settings.owner_telegram_id:
        return
    bot = get_bot()
    text = (
        f"💡 Замечаю паттерн в чате <b>{chat}</b> ({obs}× за месяц).\n\n"
        f"<b>Сейчас пришло:</b>\n<i>{_html_escape(trigger_text)}</i>\n\n"
        f"<b>Обычно ты пишешь:</b>\n<i>{_html_escape(args_text[:300])}</i>\n\n"
        f"Подготовить черновик ответа?"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✓ да, черновик", callback_data=f"pro:draft:{sig}"),
        InlineKeyboardButton(text="✗ не сейчас", callback_data=f"pro:skip:{sig}"),
        InlineKeyboardButton(text="🤫 не предлагай", callback_data=f"pro:mute:{sig}"),
    ]])
    try:
        await bot.send_message(
            chat_id=settings.owner_telegram_id,
            text=text, parse_mode="HTML", reply_markup=kb,
        )
        log.info("proactive: DM sent for pattern %s (chat=%s)", sig, chat)
    except Exception as exc:
        log.warning("proactive: DM failed: %s", exc)


def _html_escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
