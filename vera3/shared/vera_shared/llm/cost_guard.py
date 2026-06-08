"""Cost guard — жёсткий запрет paid вызовов превышающих бюджет.

Урок из Vera 2.0 ($25 burn 2026-06-01): без жёсткого cap'а cost-tracker
накапливал стейл цифры и не мог остановить burn. Vera 3.0:

- Cap проверяется ПЕРЕД каждым paid вызовом (не после)
- Cap живёт в БД per-token, не in-memory
- Глобальный cap отдельный
- Превышение → DailyBudgetExceeded, fallback на free
"""
from __future__ import annotations

import os
from datetime import date, datetime

from vera_shared.llm.registry import cost_usd


class DailyBudgetExceeded(Exception):
    """Превышен дневной бюджет (per-token или глобальный)."""

    def __init__(self, *, kind: str, limit: float, used: float, attempted: float):
        self.kind = kind  # "per_token" | "global"
        self.limit = limit
        self.used = used
        self.attempted = attempted
        super().__init__(
            f"Daily {kind} budget exceeded: used ${used:.4f} + attempt ${attempted:.4f} "
            f"> cap ${limit:.4f}"
        )


def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    """Прогноз стоимости вызова. Делегирует в registry."""
    return cost_usd(model, tokens_in, tokens_out)


def can_call_paid(
    token_tier: str,
    daily_cost_used_usd: float,
    daily_cost_cap_usd: float | None,
    estimated_cost: float,
    *,
    global_daily_used: float = 0.0,
    global_daily_cap: float | None = None,
) -> bool:
    """Можно ли совершить paid-вызов с этим токеном.

    Free и trial вызовы не проверяются (всегда True).

    Args:
        token_tier: 'free' | 'paid' | 'trial'
        daily_cost_used_usd: уже потрачено за сегодня этим токеном
        daily_cost_cap_usd: дневной cap токена (None = нет лимита, опасно)
        estimated_cost: оценка стоимости этого вызова
        global_daily_used: сколько потратили все токены сегодня
        global_daily_cap: глобальный лимит на день
    """
    if token_tier != "paid":
        return True  # free и trial — без проверки

    # Если daily_cost_cap_usd не задан — считаем 0 (т.е. блокируем).
    # Лучше отказать чем burn'ить безконтрольно.
    cap = daily_cost_cap_usd or 0.0

    if daily_cost_used_usd + estimated_cost > cap:
        return False

    if global_daily_cap is not None:
        if global_daily_used + estimated_cost > global_daily_cap:
            return False

    return True


def assert_can_call_paid(
    token_tier: str,
    daily_cost_used_usd: float,
    daily_cost_cap_usd: float | None,
    estimated_cost: float,
    *,
    global_daily_used: float = 0.0,
    global_daily_cap: float | None = None,
) -> None:
    """То же что can_call_paid но кидает исключение."""
    if not can_call_paid(
        token_tier,
        daily_cost_used_usd,
        daily_cost_cap_usd,
        estimated_cost,
        global_daily_used=global_daily_used,
        global_daily_cap=global_daily_cap,
    ):
        # Определяем какой именно cap превышен
        cap_token = daily_cost_cap_usd or 0.0
        if daily_cost_used_usd + estimated_cost > cap_token:
            raise DailyBudgetExceeded(
                kind="per_token",
                limit=cap_token,
                used=daily_cost_used_usd,
                attempted=estimated_cost,
            )
        if global_daily_cap is not None:
            raise DailyBudgetExceeded(
                kind="global",
                limit=global_daily_cap,
                used=global_daily_used,
                attempted=estimated_cost,
            )
        raise DailyBudgetExceeded(  # pragma: no cover — defensive
            kind="unknown",
            limit=0.0,
            used=daily_cost_used_usd,
            attempted=estimated_cost,
        )


def global_daily_cap_from_env() -> float | None:
    """Read VERA_DAILY_GLOBAL_CAP_USD env var. None = no cap."""
    raw = os.environ.get("VERA_DAILY_GLOBAL_CAP_USD", "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None
