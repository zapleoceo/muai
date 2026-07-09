"""Форматирование ответов для Telegram — чистые функции, БЕЗ импорта aiogram.

Вынесено отдельно, чтобы:
1. Тестировать без поднятия бота (aiogram не нужен в test-окружении).
2. Держать единственный источник экранирования — легко проверить, что каждый
   путь отправки проходит через него.
"""
from __future__ import annotations

from html import escape as _html_escape

TELEGRAM_MAX_CHARS = 4096


def format_reply(raw_answer: str, provider: str | None, cost_usd: float,
                  n_results: int, n_history: int) -> str:
    """HTML-safe текст ответа: экранирует answer/provider, добавляет footer
    курсивом, режет под лимит Telegram в 4096 символов.

    Экранирование ОБЯЗАТЕЛЬНО: LLM-ответы часто цитируют письма вида
    'From: IT Step <no-reply@itstep.org>' — без escape Telegram видит
    '<no-reply@itstep.org>' как незакрытый HTML-тег и отклоняет сообщение
    целиком ("can't parse entities: Unsupported start tag"), бот молча не
    отвечает (инцидент 2026-07-03).
    """
    safe_answer = _html_escape(raw_answer or "(пустой ответ)", quote=False)
    safe_provider = _html_escape(str(provider or "—"), quote=False)
    footer = (f"\n\n<i>via {safe_provider}, ${cost_usd:.4f}, "
              f"{n_results} событий · {n_history} реплик контекста</i>")
    max_answer = TELEGRAM_MAX_CHARS - len(footer)
    if len(safe_answer) > max_answer:
        safe_answer = safe_answer[:max(0, max_answer - 1)] + "…"
    return safe_answer + footer


def plain_fallback(raw_answer: str, provider: str | None) -> str:
    """Fallback без какого-либо форматирования (parse_mode=None у вызывающего).

    Второй рубеж защиты: даже если format_reply почему-то не спасёт (edge
    case юникода/лимитов), это гарантированно уходит без ошибок парсинга.
    """
    text = (raw_answer or "(пустой ответ)")[:4000]
    return f"{text}\n\nvia {provider or '—'}"


def format_error(exc: BaseException) -> str:
    """Текст ошибки для Telegram — БЕЗ HTML-тегов, всегда шлётся plain."""
    return f"⚠ Ошибка: {exc}"[:4000]
