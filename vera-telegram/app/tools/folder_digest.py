"""Folder digest — map-reduce саммари по всем чатам папки.

Без потери контекста внутри чата: каждый чат полностью читается и
саммируется отдельным LLM-вызовом, потом N коротких саммари
агрегируются в финальный дайджест. Возвращает структурированный dict,
который оркестратор может процитировать без обработки.
"""
import asyncio
import logging

import httpx

from app.config import get_settings
from app.tools.list_folders import list_folders
from app.tools.read_messages import read_messages

log = logging.getLogger(__name__)


_PER_CHAT_PROMPT = (
    "Ниже — сообщения за период из одного Telegram-чата. "
    "Сделай саммари в 1-3 строках: о чём говорили, какие договорённости/"
    "задачи, чего ждут. Если важных пунктов нет — напиши «без ключевых тем». "
    "Стиль: краткий, без воды, без эмодзи. Только русский язык."
)


async def folder_digest(folder_title: str, days: int = 1,
                          limit_per_chat: int = 50) -> dict:
    """Возвращает per-chat саммари + общую статистику для папки."""
    folders = await list_folders()
    match = None
    title_low = folder_title.strip().lower().replace(" ", "")
    for f in folders or []:
        if not isinstance(f, dict):
            continue
        ft = (f.get("title") or "").lower().replace(" ", "")
        if ft == title_low or title_low in ft:
            match = f
            break
    if match is None:
        return {"_error": f"folder {folder_title!r} not found",
                 "available_folders": [f.get("title") for f in folders or []
                                        if isinstance(f, dict)]}

    peer_ids = match.get("peer_ids") or []

    # Параллельно читаем все чаты (full, без compaction).
    async def _read_one(cid: int) -> dict:
        try:
            return await read_messages(
                peer=str(cid), limit=int(limit_per_chat),
                offset_days=int(days), ocr_images=False,
            )
        except Exception as exc:
            return {"chat_id": cid, "_error": str(exc)[:120]}

    read_tasks = [asyncio.create_task(_read_one(int(c))) for c in peer_ids]
    read_results = await asyncio.gather(*read_tasks)

    chats_with_msgs = [r for r in read_results
                        if isinstance(r, dict) and (r.get("messages") or [])]

    # Параллельно саммируем — каждый чат полностью видит свой LLM-вызов.
    async def _summarise(chat: dict) -> dict:
        msgs = chat.get("messages") or []
        text_block = "\n".join(
            f"[{(m.get('date') or '')[:16]}] {(m.get('from') or '?')}: "
            f"{m.get('text') or m.get('message') or ''}"
            for m in msgs
        )[:6000]  # cap per-chat input at 6k chars (~20 messages full)
        summary = await _llm_summarise(text_block, chat.get("chat_name", "?"))
        return {
            "chat_id": chat.get("chat_id"),
            "chat_name": chat.get("chat_name"),
            "messages_count": len(msgs),
            "summary": summary,
        }

    sem = asyncio.Semaphore(4)  # don't burn LLM pool on huge folders
    async def _bounded(chat: dict) -> dict:
        async with sem:
            return await _summarise(chat)

    summarised = await asyncio.gather(*[_bounded(c) for c in chats_with_msgs])

    empty_chat_names = [
        (r or {}).get("chat_name") for r in read_results
        if isinstance(r, dict) and not (r.get("messages") or [])
        and not r.get("_error")
    ]

    return {
        "folder": match.get("title"),
        "chats_total": len(peer_ids),
        "chats_with_activity": len(summarised),
        "days": days,
        "active": summarised,
        "silent_chats": [n for n in empty_chat_names if n],
    }


async def _llm_summarise(text: str, chat_name: str) -> str:
    """Прокси к LiteLLM router через vera-core. vera-telegram тут не
    держит свой LLM-клиент — все вызовы идут через единый пул в core."""
    cfg = get_settings()
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(
                f"{cfg.vera_core_url}/internal/llm/chat",
                headers={"X-Internal-Secret": cfg.internal_secret},
                json={
                    "system": _PER_CHAT_PROMPT,
                    "messages": [{"role": "user",
                                   "content": f"Чат: {chat_name}\n\n{text}"}],
                    "capability": "chat:fast",
                },
            )
        if r.status_code != 200:
            log.warning("LLM summarise failed: %s %s", r.status_code, r.text[:200])
            return f"(не удалось саммировать: {r.status_code})"
        return (r.json().get("text") or "").strip() or "(пусто)"
    except Exception as exc:
        log.warning("LLM summarise exc: %s", exc)
        return f"(ошибка LLM: {str(exc)[:80]})"
