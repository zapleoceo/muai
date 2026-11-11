"""Разбор списков токенов из env (`имя:токен,…`, `имя:проект:токен,…`) и проверка стойкости.

Один разборщик и одна проверка длины на MCP_TOKEN(S), ROOM_TOKENS и OFFICE_SERVICE_TOKENS:
правило "короче 32 символов - не токен" не должно разъезжаться между наборами.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

MIN_TOKEN_LEN = 32


def split_token_entries(raw: str | None, fields: int) -> list[list[str]]:
    """Записи через запятую, каждая порезана по ':' не больше чем на `fields` частей.

    Последняя часть - токен целиком, даже если в нём есть ':'. Пустые записи пропускаются.
    """
    items = filter(None, (p.strip() for p in (raw or "").split(",")))
    return [item.split(":", fields - 1) for item in items]


def is_strong_token(token: str) -> bool:
    return len(token) >= MIN_TOKEN_LEN


def repeated_tokens(tokens: Iterable[str]) -> set[str]:
    return {t for t, n in Counter(tokens).items() if n > 1}
