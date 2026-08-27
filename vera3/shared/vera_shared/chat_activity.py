"""Участие владельца в чате — данные для `media_policy`.

Признак объективный и уже лежит в событиях: сколько сообщений владелец сам
написал в этом чате (`direction == 'sent'`). Политика по этому числу решает,
распознавать ли фото из группы, — вместо ручного денилиста по названиям.

Кэш обязателен: решение принимается на КАЖДОМ входящем медиа, а чатов
десятки. Считать заново на каждое сообщение — это скан по jsonb-полю на
430 тысячах событий. Индекс под этот запрос ставит миграция 027.

TTL, а не «навсегда»: владелец может начать писать в чат, где раньше молчал, —
через час это увидят и фото оттуда начнут распознаваться. Наоборот тоже
работает: `scripts/media_requeue.py` пересматривает старые пропуски.
"""
from __future__ import annotations

import logging
import time

from sqlalchemy import text

from vera_shared.control import MEDIA_MIN_OWN_MESSAGES, get_int_setting
from vera_shared.db.engine import get_session

log = logging.getLogger(__name__)

#: Сколько держать посчитанное. Час — компромисс: новый чат подхватится в
#: пределах рабочей сессии, а нагрузка на БД остаётся нулевой.
TTL_S = 3600.0

#: Порог по умолчанию, если настройка не задана. Обоснование замером
#: 2026-08-27: «Кайфушники Нячанга» — 3 своих сообщения из 1735 при 235
#: авторах (шум, отсекаем), «Jakarta sales» — 16, «BEER AI Нячанг» — 17,
#: «JAKARTA <> MARKETING HQ TEAM» — 30 (настоящая работа, пропускаем).
DEFAULT_MIN_OWN_MESSAGES = 5

_cache: dict[str, tuple[int, float]] = {}


def _now() -> float:
    return time.monotonic()


def forget(chat_id: str | int | None = None) -> None:
    """Сбросить кэш — целиком или по одному чату. Для тестов и скриптов."""
    if chat_id is None:
        _cache.clear()
    else:
        _cache.pop(str(chat_id), None)


async def own_message_count(chat_id: str | int | None) -> int:
    """Сколько сообщений владелец написал в этом чате. Неизвестно → 0.

    Ошибку БД не пробрасываем: политика не имеет права уронить загрузку
    сообщений. Ноль означает «участие не подтверждено» — фото из группы не
    пойдёт на распознавание, но событие сохранится с заглушкой и причиной.
    """
    if chat_id is None:
        return 0
    key = str(chat_id)
    hit = _cache.get(key)
    if hit is not None and hit[1] > _now():
        return hit[0]
    try:
        async with get_session() as s:
            # CAST обязателен: в Postgres `->>` всегда отдаёт текст, а в
            # SQLite (на нём гоняются тесты) сохраняет тип JSON-значения, и
            # число 111 никогда не сравнится со строкой '111'.
            count = (await s.execute(text("""
                SELECT COUNT(*) FROM events
                WHERE source = 'telegram'
                  AND CAST(metadata->>'chat_id' AS TEXT) = :cid
                  AND CAST(metadata->>'direction' AS TEXT) = 'sent'
            """), {"cid": key})).scalar_one_or_none() or 0
    except Exception as e:
        log.warning("участие в чате %s не посчиталось (%s) — считаю нулём", key, e)
        return 0
    _cache[key] = (int(count), _now() + TTL_S)
    return int(count)


async def min_own_messages() -> int:
    """Порог участия. Настройка, а не константа в коде."""
    return await get_int_setting(MEDIA_MIN_OWN_MESSAGES, DEFAULT_MIN_OWN_MESSAGES)
