"""Cost guard — жёсткий запрет paid вызовов превышающих бюджет.

Урок из Vera 2.0 ($25 burn 2026-06-01): без жёсткого cap'а cost-tracker
накапливал стейл цифры и не мог остановить burn. Vera 3.0:

- Cap проверяется ПЕРЕД каждым paid вызовом (не после)
- Cap живёт в БД per-token, не in-memory (3 реплики != 3 трекера)
- Глобальный cap считается из usage_log SUM
- Превышение → DailyBudgetExceeded, fallback на free
"""
from __future__ import annotations

import os
import time
from datetime import date, datetime, timezone

from sqlalchemy import text

from vera_shared.db.engine import get_session
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


# ─── Глобальный cost от всех реплик ─────────────────────────────────────────

# Кэш на 30 секунд — компромисс между нагрузкой на БД и точностью cap'а.
# Если реальное использование чуть отстаёт от кэша — следующий tick поправит.
_global_cost_cache: dict[str, float | int] = {"value": 0.0, "fetched_at": 0.0}
_CACHE_TTL_S = 30


async def global_cost_today() -> float:
    """Сколько потрачено всеми токенами за сегодня. SUM(usage_log).

    Кэшируется на _CACHE_TTL_S чтобы не делать 100 SUM в секунду.
    Под нагрузкой 3 реплик ~30 сек погрешности на cap = ~$0.1, ок.
    """
    now = time.time()
    if now - float(_global_cost_cache["fetched_at"]) < _CACHE_TTL_S:
        return float(_global_cost_cache["value"])

    today = datetime.now(timezone.utc).date()
    async with get_session() as s:
        result = await s.execute(text(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM usage_log "
            "WHERE created_at::date >= :today"
        ), {"today": today})
        total = float(result.scalar() or 0.0)

    _global_cost_cache["value"] = total
    _global_cost_cache["fetched_at"] = now
    return total


def invalidate_global_cost_cache() -> None:
    """Сбросить кэш — вызывается после каждого записанного paid вызова в той же
    реплике, чтобы наш свежий вызов не игнорировался при следующем check."""
    _global_cost_cache["fetched_at"] = 0.0


# ─── Pre-flight checks ──────────────────────────────────────────────────────


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
    """
    if token_tier != "paid":
        return True

    # Если daily_cost_cap_usd не задан — считаем 0 (т.е. блокируем).
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
