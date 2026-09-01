"""Наивный UTC — одно имя вместо `datetime.now(UTC).replace(tzinfo=None)`.

Все datetime-колонки в БД наивные и хранят UTC (см. docs/domain-model.md), а
дашборд помечает их 'Z' на рендере (`render.local_dt`). Поэтому «сейчас» в
коде — это именно наивный UTC, и записывать его надо одинаково везде.

Раньше это была `datetime.utcnow()` в 53 местах. Она deprecated с Python
3.12 (проект на 3.12) и снята с плана поддержки, а буквальная замена
`datetime.now(UTC).replace(tzinfo=None)` — три вызова в строке, которые
разъезжаются при первом же копировании: достаточно забыть `.replace()`, и в
наивную колонку поедет aware-datetime.
"""
from __future__ import annotations

from datetime import UTC, datetime


def utc_naive_now() -> datetime:
    """Текущее время UTC без tzinfo — ровно то, что кладётся в БД."""
    return datetime.now(UTC).replace(tzinfo=None)
