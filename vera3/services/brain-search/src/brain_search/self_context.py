"""«Кто я и что у меня подключено» — блок, который уходит в промпт синтеза.

Нужен, чтобы на вопрос про саму Веру она отвечала по своей реальной
конфигурации, а не пересказывала письмо, где что-то похожее упомянуто.

Кэш обязателен: `COUNT(*) FROM events` и `GROUP BY source` — проходы по
таблице на сотни тысяч строк, и без кэша они шли бы на КАЖДЫЙ /search.
"""
from __future__ import annotations

import time
from typing import Any

from sqlalchemy import func, select, text
from vera_shared.db.engine import get_session
from vera_shared.db.models import EventRow
from vera_shared.db.models_sources import GmailAccountRow

TTL_S = 60
_cache: dict[str, Any] = {"value": None, "fetched_at": 0.0}


def forget() -> None:
    """Сбросить кэш — для тестов и после подключения нового источника."""
    _cache["value"] = None
    _cache["fetched_at"] = 0.0


async def self_context(now: float | None = None) -> str:
    now = time.time() if now is None else now
    if _cache["value"] and (now - _cache["fetched_at"] < TTL_S):
        return _cache["value"]

    async with get_session() as s:
        gmail_accs = (await s.execute(
            select(GmailAccountRow.email)
            .where(GmailAccountRow.is_active.is_(True))
            .order_by(GmailAccountRow.id)
        )).scalars().all()
        total_events = (await s.execute(select(func.count(EventRow.id)))).scalar() or 0
        per_src = (await s.execute(text(
            "SELECT source, COUNT(*) FROM events GROUP BY source ORDER BY 2 DESC"
        ))).all()

    lines = ["Я — Vera 3.0, личная память Димы.",
             f"Всего событий в моём мозге: {total_events:,}.",
             "",
             "Подключённые ИСТОЧНИКИ (по которым я реально читаю входящий поток):"]
    if gmail_accs:
        lines.append(f"• Gmail (через OAuth) — {len(gmail_accs)} ящика:")
        lines.extend(f"  – {email}" for email in gmail_accs)
    else:
        lines.append("• Gmail — нет подключённых ящиков.")
    lines += ["", "По источникам в БД событий (накоплено за всё время):"]
    lines.extend(f"• {src}: {cnt:,}" for src, cnt in per_src)

    _cache["value"] = "\n".join(lines)
    _cache["fetched_at"] = now
    return _cache["value"]
