"""Экспоненциальная пауза перед повтором: одна формула на всех, единицы — у вызывающего."""
from __future__ import annotations


def exp_backoff(attempt: int, base: float, *, cap: float | None = None,
                max_exponent: int | None = None, jitter: float = 1.0) -> float:
    """`base * 2^(attempt-1)` (attempt<=1 — сама `base`), обрезка `cap`, затем множитель `jitter`."""
    exponent = max(attempt - 1, 0)
    if max_exponent is not None:
        exponent = min(exponent, max_exponent)
    delay = base * 2 ** exponent
    if cap is not None:
        delay = min(delay, cap)
    return delay * jitter
