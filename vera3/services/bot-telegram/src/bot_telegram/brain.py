"""Вопрос «мозгу» и запись реплик разговора — общий путь текста и голоса.

Вынесено из `bot.py`, чтобы голосовое поручение шло РОВНО тем же путём, что
текстовое сообщение боту: тот же запрос в brain-search, та же история
разговора, та же запись ответа событием. Две копии этого пути разъехались бы
при первой же правке. Без импорта aiogram — тестируется без бота.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime

import httpx
from vera_shared.timeutil import utc_naive_now

log = logging.getLogger(__name__)

SEARCH_URL = os.environ.get("SEARCH_URL", "http://brain-search:8000")
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://gateway:8000")
INTERNAL_SECRET = os.environ.get("INTERNAL_SECRET", "")


class BrainError(RuntimeError):
    """Поиск ответил не 200 — показать владельцу код, а не пустоту."""


@dataclass(frozen=True)
class BrainAnswer:
    raw: str
    provider: str
    cost_usd: float
    n_results: int
    n_history: int


async def ask_brain(query: str, chat_id: int, user_id: int) -> BrainAnswer:
    async with httpx.AsyncClient(timeout=120.0) as c:
        r = await c.post(
            f"{SEARCH_URL}/search",
            json={
                "q": query,
                "limit": 15,
                "conversation": {"chat_id": chat_id, "user_id": user_id},
            },
            headers={"X-Internal-Secret": INTERNAL_SECRET},
        )
    if r.status_code != 200:
        raise BrainError(f"HTTP {r.status_code}")
    data = r.json()
    return BrainAnswer(
        raw=data.get("answer", "(пустой ответ)"),
        provider=data.get("provider") or "—",
        cost_usd=data.get("cost_usd", 0.0),
        n_results=len(data.get("results", [])),
        n_history=data.get("history_used", 0),
    )


async def save_event(chat_id: int, msg_id: int, role: str, content: str,
                     sender_id: int | None = None,
                     occurred_at: datetime | None = None) -> None:
    """Записать реплику разговора в events table через gateway.

    role: 'user' (Dima) или 'vera' (bot's answer).
    """
    payload = {
        "source": "vera_chat",
        "source_event_id": f"tg:{chat_id}:{msg_id}:{role}",
        "account": f"chat:{chat_id}",
        "category": role,
        "content_text": content[:8000],
        "occurred_at": (occurred_at or utc_naive_now()).isoformat(),
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
