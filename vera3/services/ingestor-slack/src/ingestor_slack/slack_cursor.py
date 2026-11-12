"""Чистые решения поллера о курсоре и нечитаемых каналах — без БД и часов."""
from __future__ import annotations

import os
from datetime import datetime, timedelta

from vera_shared.ingest_policy import slack_channel_unreadable

# Как часто перепроверять канал, который в прошлый раз оказался нечитаемым.
# Не «никогда»: в канал могут вернуть, и тогда он должен ожить сам.
UNREADABLE_RETRY_H = int(os.environ.get("SLACK_UNREADABLE_RETRY_H", "24"))


def skip_unreadable(last_error: str | None, last_polled_at: datetime | None,
                    now: datetime, retry_after_h: int = UNREADABLE_RETRY_H) -> bool:
    """Пропустить канал в этом прогоне, потому что он не читается?

    Чистая функция — решение принимается по строке прошлой ошибки и времени
    последней попытки, без БД и часов.
    """
    if not slack_channel_unreadable(last_error) or last_polled_at is None:
        return False
    return (now - last_polled_at) < timedelta(hours=retry_after_h)


def newest_ts(messages: list[dict]) -> str | None:
    return max((str(m.get("ts") or "") for m in messages), default="") or None
