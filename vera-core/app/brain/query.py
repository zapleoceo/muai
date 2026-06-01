"""Brain query — read from graph + Event store, not from source APIs.

Following VERA.md hard rule «Everything Vera knows lives in the graph»:
when Vera needs to summarise/recall, she reads the BRAIN, not the
original Telegram/Gmail API. Source APIs are for write-actions (send,
archive) — never for read.

Two functions exported as tools:
  vera_query_events(filters)        — raw events matching filters
  vera_folder_digest(folder, days)  — map-reduce per-chat summary
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, desc, or_, select

from vera_shared.db.engine import get_session
from vera_shared.db.models import Event
from vera_shared.llm.router import chat as llm_chat

log = logging.getLogger(__name__)


async def vera_query_events(
    source: str | None = None,
    account: str | None = None,
    folder: str | None = None,
    chat_name: str | None = None,
    person: str | None = None,
    query: str | None = None,
    days: int | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 200,
) -> dict:
    """Query Event store by structured filters.

    Args:
        query: optional free-text — matches if substring appears in
               content_text (case-insensitive). Use for searching topics
               like "выплата", "счёт", "доставка" without knowing the chat.
        All other filters AND-combined.

    Returns
      {total, returned, events:[{id, source, occurred_at, chat,
                                  person, text, direction}, ...]}
    """
    where = []
    if source:
        where.append(Event.source == source)
    if account:
        where.append(Event.account == account)
    if query:
        where.append(Event.content_text.ilike(f"%{query.strip()}%"))
    if days is not None:
        where.append(Event.occurred_at >= datetime.utcnow() - timedelta(days=int(days)))
    if since:
        try:
            where.append(Event.occurred_at >= datetime.fromisoformat(since))
        except ValueError:
            pass
    if until:
        try:
            where.append(Event.occurred_at <= datetime.fromisoformat(until))
        except ValueError:
            pass

    async with get_session() as s:
        q = select(Event).where(and_(*where)) if where else select(Event)
        q = q.order_by(desc(Event.occurred_at)).limit(min(int(limit), 1000))
        rows = (await s.execute(q)).scalars().all()

    folder_norm = _norm(folder) if folder else None
    chat_norm = _norm(chat_name) if chat_name else None
    person_norm = _norm(person) if person else None

    out: list[dict] = []
    for e in rows:
        hints = e.entity_hints or []
        meta = e.metadata_ or {}
        if folder_norm:
            f = _norm(meta.get("folder", "")) if isinstance(meta, dict) else ""
            f_in_hints = any(_norm(h.get("identifier", "")) == folder_norm
                              for h in hints if h.get("type") == "folder")
            if folder_norm not in f and not f_in_hints:
                continue
        if chat_norm:
            ct = _norm(meta.get("chat_title", "")) if isinstance(meta, dict) else ""
            ct_hint = any(chat_norm in _norm(h.get("name", ""))
                           for h in hints if h.get("type") == "chat")
            if chat_norm not in ct and not ct_hint:
                continue
        if person_norm:
            p_match = any(person_norm in _norm(h.get("name", ""))
                            or person_norm in _norm(h.get("identifier", ""))
                            for h in hints if h.get("type") == "person")
            if not p_match:
                continue

        chat_label = (meta.get("chat_title") if isinstance(meta, dict) else None) or \
                      next((h.get("name") for h in hints if h.get("type") == "chat"),
                            e.account)
        person_label = next((h.get("name") for h in hints if h.get("type") == "person"),
                              None)
        out.append({
            "id": e.id,
            "source": e.source,
            "occurred_at": e.occurred_at.isoformat() if e.occurred_at else None,
            "chat": chat_label,
            "person": person_label,
            "direction": (meta.get("direction") if isinstance(meta, dict) else None),
            "text": (e.content_text or "")[:600],
        })

    return {
        "total_matched": len(out),
        "returned": len(out),
        "events": out,
    }


async def vera_folder_digest(folder: str, days: int = 1) -> dict:
    """Map-reduce per-chat summary over events ALREADY in the brain.

    No TG API roundtrip — reads only from local Event store. Each chat's
    events bundled, sent to LLM for 1-3 line summary, results aggregated.
    """
    q = await vera_query_events(folder=folder, days=days, limit=1000)
    events = q.get("events") or []
    if not events:
        return {"folder": folder, "days": days,
                "chats_total": 0, "chats_with_activity": 0,
                "note": ("в графе нет событий с metadata.folder=" + folder +
                          f" за последние {days} дн. Может papka не та "
                          "(проверь telegram_list_folders), или backfill ещё "
                          "не добежал.")}

    by_chat: dict[str, list[dict]] = {}
    for e in events:
        by_chat.setdefault(e.get("chat") or "?", []).append(e)

    sem = asyncio.Semaphore(4)
    async def _summarise(chat_name: str, chat_events: list[dict]) -> dict:
        text_block = "\n".join(
            f"[{(e.get('occurred_at') or '')[:16]}] "
            f"{e.get('person') or '?'}"
            f"{' (sent)' if e.get('direction') == 'sent' else ''}: "
            f"{e.get('text') or ''}"
            for e in chat_events
        )[:6000]
        async with sem:
            try:
                summary = await llm_chat(
                    messages=[{"role": "user",
                                "content": f"Чат: {chat_name}\n\n{text_block}"}],
                    system=(
                        "Кратко (1-3 строки) что обсуждали и какие задачи/"
                        "договорённости. Без воды, без эмодзи. Только русский. "
                        "Если ничего важного — «без ключевых тем»."
                    ),
                    capability="chat:fast",
                )
            except Exception as exc:
                summary = f"(LLM ошибка: {str(exc)[:80]})"
        return {
            "chat": chat_name,
            "messages_count": len(chat_events),
            "summary": summary.strip(),
        }

    tasks = [_summarise(name, evs) for name, evs in by_chat.items()]
    summaries = await asyncio.gather(*tasks)

    return {
        "folder": folder,
        "days": days,
        "chats_with_activity": len(summaries),
        "events_total": len(events),
        "active": summaries,
    }


def _norm(s: str) -> str:
    return "".join(c for c in (s or "").lower() if c.isalnum())
