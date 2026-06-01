"""Hard cost-ceiling guard — refuses paid calls that would exceed daily budget."""
import os

import pytest

from vera_shared.llm.cost_guard import (
    DailyBudgetExceeded, check_and_reserve, current_spend, estimate_cost,
    reset_window,
)


@pytest.fixture(autouse=True)
def _isolate():
    reset_window()
    yield
    reset_window()


def test_estimate_known_model():
    # gemini-3.5-flash: $1.50 in / $9 out per 1M
    cost = estimate_cost("gemini-3.5-flash", 1_000_000, 100_000)
    assert cost == pytest.approx(1.50 + 0.9, abs=0.001)


def test_estimate_unknown_model_returns_zero():
    assert estimate_cost("unknown-model-name", 1_000_000, 1_000_000) == 0.0


def test_estimate_handles_provider_prefix():
    # "gemini/gemini-3.5-flash" should still resolve to the model name
    cost = estimate_cost("gemini/gemini-3.5-flash", 1_000, 1_000)
    assert cost > 0.0


def test_free_model_does_not_consume_budget():
    cost = pytest.run = None  # noqa
    import asyncio
    asyncio.run(check_and_reserve("deepseek-chat", 10_000_000, 10_000_000))
    spent, _ = current_spend()
    assert spent == 0.0


@pytest.mark.asyncio
async def test_check_under_budget_passes():
    os.environ["VERA_DAILY_LIMIT_USD"] = "10.0"
    cost = await check_and_reserve("gemini-3.5-flash", 1_000, 100)
    assert cost > 0
    spent, limit = current_spend()
    assert spent == cost
    assert limit == 10.0


@pytest.mark.asyncio
async def test_check_over_budget_raises():
    os.environ["VERA_DAILY_LIMIT_USD"] = "0.001"
    with pytest.raises(DailyBudgetExceeded):
        await check_and_reserve("gemini-3.5-flash", 1_000_000, 100_000)


@pytest.mark.asyncio
async def test_repeated_calls_accumulate():
    os.environ["VERA_DAILY_LIMIT_USD"] = "1.0"
    await check_and_reserve("gemini-3.5-flash", 100_000, 10_000)
    spent_1, _ = current_spend()
    await check_and_reserve("gemini-3.5-flash", 100_000, 10_000)
    spent_2, _ = current_spend()
    assert spent_2 == pytest.approx(spent_1 * 2, rel=0.01)
