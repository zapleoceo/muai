"""Признак живости воркера для HEALTHCHECK.

У brain-triage нет HTTP-порта, поэтому healthcheck'а не было вовсе — а это
единственный сервис, который масштабируется репликами и у которого есть
реальный сценарий «процесс жив, работы не делает»: исчерпание пула
соединений фоновыми задачами (см. docs/brain.md, rel-extract concurrency).
Такая реплика для `docker compose ps` и для монитора выглядит здоровой:
оба считают контейнеры и рестарты, а не прогресс.

Файл, а не запись в БД: healthcheck не должен зависеть от самой базы, иначе
кратковременная недоступность Postgres уронит разом все пять реплик, хотя
чинить надо не их.
"""
from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path

BEAT_FILE = Path(os.environ.get("TRIAGE_HEARTBEAT_FILE", "/tmp/vera3-triage.beat"))

# Полный цикл: сон при пустой очереди (POLL_INTERVAL_S) + разбор батча. Порог
# должен быть заметно больше самого медленного нормального прохода, иначе
# healthcheck будет ронять занятые реплики. Батч ограничен таймаутами
# триажа (120с одиночный / 180с групповой), поэтому 300с — с запасом.
STALE_AFTER_S = float(os.environ.get("TRIAGE_HEARTBEAT_STALE_S", "300"))


def beat(now: float | None = None) -> None:
    """Отметить, что цикл прошёл ещё раз. Никогда не бросает: сбой записи
    heartbeat'а не имеет права останавливать разбор очереди."""
    with contextlib.suppress(OSError):
        BEAT_FILE.write_text(str(now if now is not None else time.time()), encoding="utf-8")


def seconds_since_beat(now: float | None = None) -> float | None:
    """Сколько секунд назад был последний цикл. None — отметки ещё нет."""
    try:
        raw = BEAT_FILE.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None
    try:
        return (now if now is not None else time.time()) - float(raw)
    except ValueError:
        return None


def is_alive(now: float | None = None) -> bool:
    """Свежая ли отметка. Отсутствие отметки — ЖИВ: контейнер только что
    поднялся и первый цикл ещё не закончил; на это есть start-period."""
    age = seconds_since_beat(now)
    return age is None or age < STALE_AFTER_S


def _cli() -> int:
    return 0 if is_alive() else 1


if __name__ == "__main__":
    raise SystemExit(_cli())
