"""Одна строка «всё ли работает» для главной — из чисел, которые дашборд
уже считает (кэш статистики и обзор источников), без новых запросов."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from dashboard.source_registry import CATALOG

# Очередь триажа, при которой «идёт, но отстаёт» превращается в «встало».
_STALLED_QUEUE = 500


@dataclass(frozen=True)
class Health:
    level: str  # ok | warn | err
    text: str


def silent_sources(overview: dict[str, dict[str, Any]], now: datetime) -> list[str]:
    """Названия источников с опросом, которые молчат дольше своего порога."""
    out: list[str] = []
    for src in CATALOG:
        stat = overview.get(src.key)
        if src.live_min is None or not stat or not stat.get("total"):
            continue
        last = stat.get("last")
        limit = src.warn_min or src.live_min * 4
        if last is None or (now - last).total_seconds() / 60 >= limit:
            out.append(src.title)
    return out


def assess(stats: dict[str, Any], overview: dict[str, dict[str, Any]],
           now: datetime) -> Health:
    queue = stats["pending"] + stats["error"]
    problems: list[str] = []
    level = "ok"
    if queue >= _STALLED_QUEUE and stats["triage_1h"] == 0:
        level = "err"
        problems.append(f"разбор очереди стоит ({queue:,} ждут)")
    if stats["error"] or stats["dead"]:
        level = level if level == "err" else "warn"
        problems.append(f"сбоев разбора: {stats['error'] + stats['dead']:,}")
    silent = silent_sources(overview, now)
    if silent:
        level = level if level == "err" else "warn"
        problems.append("молчат: " + ", ".join(silent))
    if level == "ok":
        return Health("ok", "Всё работает")
    head = "Что-то не так" if level == "err" else "Есть замечания"
    return Health(level, f"{head}: " + "; ".join(problems))
