"""Флаг ROOM_SINGLE_EXECUTOR (дизайн §8): off | on | freeze, читается один раз за процесс.

В PR-B1 достижим только `off`. `on` и `freeze` требуют ключа аренды K и прокси (PR-C1/C2):
без них «K обязателен для новых аренд» невыполнимо. Поэтому любое значение, кроме `off`,
при первом чтении даёт `SingleExecutorUnsupported`: процесс падает громко, а не работает
в полурежиме. Чистая логика ворот для `on`/`freeze` (`claim_gate.check_new_lease(mode=…)`)
и сторожа (`watchdog_rules.decide(mode=…)`) принимает режим явным аргументом и покрыта тестами.
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

Mode = Literal["off", "on", "freeze"]
MODES = ("off", "on", "freeze")
ENV = "ROOM_SINGLE_EXECUTOR"


class SingleExecutorUnsupported(NotImplementedError):
    """Режим on/freeze включён до готовности PR-C1/C2."""


def parse_mode(raw: str | None) -> Mode:
    value = (raw or "off").strip().lower() or "off"
    if value not in MODES:
        raise ValueError(f"{ENV} must be one of {', '.join(MODES)}, got {raw!r}")
    return value  # type: ignore[return-value]


@lru_cache(maxsize=1)
def single_executor_mode() -> Mode:
    """Режим из окружения; кэшируется на процесс. Не `off` -> SingleExecutorUnsupported."""
    mode = parse_mode(os.environ.get(ENV))
    if mode != "off":
        raise SingleExecutorUnsupported(
            f"{ENV}={mode} needs the lease key and proxy (PR-C1/C2); only 'off' is available")
    return mode
