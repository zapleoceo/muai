"""Живой остаток распознавания медиа для дашборда.

`scripts/media_requeue.py` считает остаток раз в три часа и кладёт в
`app_control`. Для «сколько всего медиа проходит политику» этого хватает — цифра
меняется медленно. Для «сколько ещё не распознано» не хватало: файл распознан
минуту назад, а панель до следующего прогона показывала старое число и выглядела
как затык (26.09.2026 — «1 осталось» при пустой очереди).

Считаем поштучно только НЕраспознанное: таких строк тысячи, а не сотни тысяч, и
под них есть частичный индекс (миграция 034). Участие владельца в чате берётся
из `chat_activity` с его часовым кэшем, так что запрос к базе ровно один.
"""
from __future__ import annotations

import logging
import time

from sqlalchemy import bindparam, text

from vera_shared.chat_activity import own_message_count
from vera_shared.db.engine import get_session
from vera_shared.media_policy import RECOGNIZED_MEDIA_KINDS, media_skip_reason

log = logging.getLogger(__name__)

#: Панель опрашивает прогресс каждые 10 секунд. Полминуты кэша — цифра живая
#: на глаз, а запрос уходит в базу вдвое реже открытых вкладок.
TTL_S = 30.0

_cache: tuple[int, float] | None = None

#: Тот же набор условий стоит в предикате частичного индекса (миграция 034).
#: Меняешь здесь — меняй и там, иначе запрос уедет в скан всей таблицы.
_UNRECOGNIZED_SQL = """
    SELECT CAST(metadata->>'chat_id' AS TEXT) AS chat_id,
           metadata->>'chat_kind'  AS chat_kind,
           metadata->>'media_kind' AS media_kind,
           COUNT(*) AS n
    FROM events
    WHERE metadata->>'media_kind' IN :kinds
      AND (metadata->>'media_recognition' IS NULL
           OR metadata->>'media_recognition' = 'failed')
      AND COALESCE(metadata->>'media_permanent', 'false') <> 'true'
    GROUP BY 1, 2, 3
"""


def forget() -> None:
    """Сбросить кэш остатка. Для тестов и ручных прогонов."""
    global _cache
    _cache = None


async def unrecognized_left(min_own: int) -> int:
    """Сколько медиа политика пропускает и они ещё не распознаны."""
    global _cache
    if _cache is not None and _cache[1] > time.monotonic():
        return _cache[0]

    stmt = text(_UNRECOGNIZED_SQL).bindparams(
        bindparam("kinds", expanding=True))
    async with get_session() as s:
        rows = (await s.execute(
            stmt, {"kinds": sorted(RECOGNIZED_MEDIA_KINDS)})).mappings().all()

    left = 0
    for row in rows:
        own = await own_message_count(row["chat_id"])
        if media_skip_reason(row["media_kind"], row["chat_kind"],
                             own_messages=own, min_own_messages=min_own):
            continue
        left += int(row["n"])
    _cache = (left, time.monotonic() + TTL_S)
    return left
