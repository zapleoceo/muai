"""Свежесть потока источника — один расчёт для точки, плашки и статусной строки.

Раньше те же пороги считались в трёх местах (`health`, `sources_view`,
`sources_routes`) и расходились в мелочах. Тишина — не поломка: суббота в
Slack и ночь в Telegram выглядят как «молчит 6 ч», но это норма, поэтому
между «живой» и «молчит» есть нейтральное «тихо», которое тревогой не
считается. Тревога — только после `warn_min` источника из каталога.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from dashboard.source_registry import Source

LIVE = "live"
QUIET = "quiet"
SILENT = "silent"
EMPTY = "empty"
NO_POLLING = "no_polling"


@dataclass(frozen=True)
class Freshness:
    state: str
    minutes: int = 0


def silence_limit_min(src: Source) -> int | None:
    if src.live_min is None:
        return None
    return src.warn_min or src.live_min * 4


def freshness_of(src: Source, last: datetime | None, now: datetime) -> Freshness:
    limit = silence_limit_min(src)
    if src.live_min is None or limit is None:
        return Freshness(NO_POLLING)
    if last is None:
        return Freshness(EMPTY)
    mins = max(0, int((now - last).total_seconds() / 60))
    if mins < src.live_min:
        return Freshness(LIVE, mins)
    return Freshness(QUIET if mins < limit else SILENT, mins)
