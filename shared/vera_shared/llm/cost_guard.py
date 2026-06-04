"""Hard daily cost ceiling for paid LLM keys.

Before any call goes out on a paid key, check the rolling 24-hour spend
against VERA_DAILY_LIMIT_USD. If exceeded, refuse the call with
DailyBudgetExceeded — caller's fallback chain picks free keys instead.

This is a safety net, not optimisation. The previous design relied on
LiteLLM's response_cost field (which is silently wrong for new models —
gemini-3.5-flash is reported at 2.5 pricing, 20× under-counted) AND
missed the entire Graphiti path (it bypasses LiteLLM entirely). Result
on 2026-06-01: $10 burned in a few hours, invisible in our metrics.

This module fixes the visibility gap with HARDCODED pricing tables that
we update by hand when models change. Better to refuse a call than to
silently bill the user.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta

log = logging.getLogger(__name__)


from vera_shared.llm.registry import cost_usd as _registry_cost_usd


class DailyBudgetExceeded(RuntimeError):
    """Raised when a paid call would push 24h spend over the configured limit."""


def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    """Delegates to the single source of truth in `vera_shared.llm.registry`.
    Kept as a function so existing imports don't break.
    """
    return _registry_cost_usd(model, tokens_in, tokens_out)


_lock = asyncio.Lock()
_window_start: datetime | None = None
_spend_window: float = 0.0


def _limit_usd() -> float:
    """Daily ceiling in USD. Default $1/day. Override with env."""
    try:
        return float(os.environ.get("VERA_DAILY_LIMIT_USD", "1.0"))
    except ValueError:
        return 1.0


async def check_and_reserve(model: str, tokens_in: int, tokens_out: int) -> float:
    """Call BEFORE making the request (you'll have to predict tokens_in
    from the prompt size and use a conservative tokens_out estimate, e.g.
    your max_tokens param). Raises DailyBudgetExceeded if the projected
    cost would exceed the limit.

    Returns the projected cost so the caller can log it.
    """
    global _window_start, _spend_window
    projected = estimate_cost(model, tokens_in, tokens_out)
    if projected <= 0:
        return 0.0
    async with _lock:
        now = datetime.utcnow()
        if _window_start is None or now - _window_start > timedelta(hours=24):
            _window_start = now
            _spend_window = 0.0
        if _spend_window + projected > _limit_usd():
            raise DailyBudgetExceeded(
                f"Refusing {model} call: projected ${projected:.4f} would push "
                f"24h spend from ${_spend_window:.4f} over the ${_limit_usd():.2f} cap. "
                f"Override with VERA_DAILY_LIMIT_USD env var."
            )
        _spend_window += projected
        return projected


def current_spend() -> tuple[float, float]:
    """Returns (spent_in_window, limit). For monitoring/dashboards."""
    return _spend_window, _limit_usd()


def reset_window() -> None:
    """Manual reset (testing or operator override)."""
    global _window_start, _spend_window
    _window_start = None
    _spend_window = 0.0
