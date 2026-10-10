"""Чистые правила переходов тихого режима; время — наивный UTC, как в БД.

Единственное реализованное прочтение «после полуночи»: срок ожидания (wait10) истёк,
а его местное время (Asia/Ho_Chi_Minh) лежит в окне [00:00, 00:00 + QUIET_FOR) того же
календарного дня, что и отправка вопроса. Отдельной дневной границы нет: окно выведено
из утверждённых 8 часов тишины. Срок, перешедший через полночь (23:55 -> 00:05), считается
тихим только при OWNER_QUIET_COUNT_CROSS_MIDNIGHT=1 (предложение, не утверждено).
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, time, timedelta, timezone

WAIT_FOR = timedelta(minutes=10)
QUIET_FOR = timedelta(hours=8)
#: Asia/Ho_Chi_Minh без перехода на летнее время — фиксированное смещение, tzdata не нужен.
LOCAL_TZ = timezone(timedelta(hours=7), "Asia/Ho_Chi_Minh")
_WINDOW_END = time(QUIET_FOR.seconds // 3600)


def quiet_enabled() -> bool:
    return os.environ.get("OWNER_QUIET_ENABLED", "0") == "1"


def count_cross_midnight() -> bool:
    return os.environ.get("OWNER_QUIET_COUNT_CROSS_MIDNIGHT", "0") == "1"


def to_local(utc_naive: datetime) -> datetime:
    return utc_naive.replace(tzinfo=UTC).astimezone(LOCAL_TZ)


def hhmm(utc_naive: datetime) -> str:
    return f"{to_local(utc_naive):%H:%M}"


def timeout_goes_quiet(deadline: datetime, *, cross_midnight: bool | None = None) -> bool:
    """Истёкший wait10 -> тишина (True) или обратно в active (False)."""
    deadline_local = to_local(deadline)
    sent_local = to_local(deadline - WAIT_FOR)
    if sent_local.date() != deadline_local.date():
        return count_cross_midnight() if cross_midnight is None else cross_midnight
    return deadline_local.time() < _WINDOW_END
