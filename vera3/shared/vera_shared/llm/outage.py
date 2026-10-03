"""Брейкер на сбой самого брокера (5xx / сеть) с экспоненциальной паузой.

Инцидент 2026-10: брокер отдавал 500 на /v1/jobs и /v1/embed, ~16 400 отказов
за 77 минут — клейм-цикл триажа возвращал события в pending и бил заново через
5с. Кулдауны circuit.py ловят только «кап бюджета» и «нет провайдера»; прочие
ошибки не записывались никуда.

Состояние в памяти процесса и общее для всех capability: падает брокер, а не
пул. После OUTAGE_THRESHOLD сбоев подряд вызовы отклоняются мгновенно на
30с → 60с → … → 5 мин; первый же успех закрывает брейкер. Пока брейкер открыт,
сбои уже летящих вызовов не удлиняют паузу. Лог — по одному WARNING на смену
состояния, не на вызов.
"""
from __future__ import annotations

import logging
import re
import time

log = logging.getLogger(__name__)

OUTAGE_THRESHOLD = 3
BASE_PAUSE_S = 30.0
MAX_PAUSE_S = 300.0

_OUTAGE_MESSAGE = re.compile(r"broker (?:poll )?5\d\d\b|broker network")

_failures = 0
_level = 0
_open_until = 0.0


def is_outage_error(message: str) -> bool:
    return bool(_OUTAGE_MESSAGE.search(message or ""))


def pause_for(level: int) -> float:
    return min(BASE_PAUSE_S * 2 ** level, MAX_PAUSE_S)


def outage_remaining_s() -> float:
    return max(0.0, _open_until - time.monotonic())


def note_broker_outage(error_message: str) -> None:
    global _failures, _level, _open_until
    now = time.monotonic()
    if now < _open_until:
        return
    _failures += 1
    if _failures < OUTAGE_THRESHOLD:
        return
    pause = pause_for(_level)
    _level += 1
    _open_until = now + pause
    log.warning("LLM broker outage circuit OPEN for %.0fs after %d consecutive "
                "failures (%s)", pause, _failures, error_message[:120])


def note_broker_ok() -> None:
    global _failures, _level, _open_until
    if _level:
        log.info("LLM broker outage circuit CLOSED (successful call)")
    _failures = 0
    _level = 0
    _open_until = 0.0


def reset_outage() -> None:
    """Для тестов."""
    global _failures, _level, _open_until
    _failures = _level = 0
    _open_until = 0.0
