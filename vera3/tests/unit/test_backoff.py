"""exp_backoff повторяет прежние формулы четырёх вызывающих бит в бит."""
from __future__ import annotations

import random
from datetime import timedelta

import pytest
from vera_shared.backoff import exp_backoff

ATTEMPTS = range(1, 40)
FACTORS = [0.0, 0.1234, 0.5, 0.999999, 1.0]


def test_plain_doubling_and_floor_at_first_attempt() -> None:
    assert [exp_backoff(n, 30) for n in (0, 1, 2, 3)] == [30, 30, 60, 120]


def test_cap_applies_before_jitter() -> None:
    assert exp_backoff(30, 5.0, cap=900.0, jitter=0.5) == 450.0


def test_max_exponent_limits_growth() -> None:
    assert exp_backoff(100, 1, max_exponent=3) == 8


@pytest.mark.parametrize("u", [0.8, 0.95, 1.0, 1.1734, 1.2])
def test_dispatcher_backoff_s_matches_old_formula(monkeypatch: pytest.MonkeyPatch, u: float) -> None:
    from bot_telegram import dispatcher
    monkeypatch.setattr(dispatcher.random, "uniform", lambda a, b: u)
    for n in ATTEMPTS:
        assert dispatcher.backoff_s(n) == int(dispatcher.RECEIPT_WAIT_S * 2 ** (n - 1) * u)


def test_dispatcher_jitter_range_unchanged() -> None:
    from bot_telegram import dispatcher
    random.seed(7)
    pauses = [dispatcher.backoff_s(1) for _ in range(500)]
    assert min(pauses) >= int(600 * 0.8) and max(pauses) <= int(600 * 1.2)


@pytest.mark.parametrize("r", FACTORS)
def test_events_deliver_matches_old_formula(r: float) -> None:
    from vera_mcp.events_deliver import BASE_DELAY_S, MAX_DELAY_S, backoff_delay
    for n in range(0, 40):
        old = min(MAX_DELAY_S, BASE_DELAY_S * 2 ** max(n - 1, 0)) * (0.5 + r / 2)
        assert backoff_delay(n, rand=lambda: r) == old


def test_pair_roles_matches_old_formula() -> None:
    from vera_shared.graph.pair_roles_store import (
        BACKOFF_BASE_HOURS,
        BACKOFF_MAX_DAYS,
        backoff,
    )
    for n in range(0, 60):
        old = min(timedelta(hours=BACKOFF_BASE_HOURS * 2 ** min(max(0, n - 1), 12)),
                  timedelta(days=BACKOFF_MAX_DAYS))
        assert backoff(n) == old


def test_voice_retry_delay_matches_old_formula() -> None:
    from vera_shared.voice_commands import RETRY_BASE_S, retry_delay
    for n in range(0, 40):
        assert retry_delay(n) == timedelta(seconds=RETRY_BASE_S * 2 ** max(0, n - 1))
