"""Тесты cost guard — 100% coverage обязательно.

Это самая критичная часть после регистра — она защищает от burn'ов.
"""
from __future__ import annotations

import pytest

from vera_shared.llm.cost_guard import (
    DailyBudgetExceeded,
    assert_can_call_paid,
    can_call_paid,
    estimate_cost,
    global_daily_cap_from_env,
)


class TestEstimateCost:
    def test_delegates_to_registry(self):
        # Просто sanity — что функция возвращает float и не падает
        cost = estimate_cost("gemini-2.5-flash", 1_000_000, 100_000)
        assert isinstance(cost, float)
        assert cost > 0

    def test_free_model_returns_zero(self):
        cost = estimate_cost("gpt-oss-120b", 1_000_000, 1_000_000)
        assert cost == 0.0


class TestCanCallPaidBasic:
    def test_free_tier_always_allowed(self):
        # Free и trial — не блокируются никогда
        assert can_call_paid("free", 0, 0, 999.0) is True
        assert can_call_paid("free", 999.0, 0.01, 999.0) is True

    def test_trial_tier_always_allowed(self):
        assert can_call_paid("trial", 999.0, 0.01, 100.0) is True

    def test_paid_under_cap_allowed(self):
        # $4 потрачено, cap $5, попытка стоит $0.10 → 4.10 < 5 → OK
        assert can_call_paid("paid", 4.0, 5.0, 0.10) is True

    def test_paid_at_cap_blocked(self):
        # $5 потрачено, cap $5 → новый вызов превышает
        assert can_call_paid("paid", 5.0, 5.0, 0.01) is False

    def test_paid_over_cap_blocked(self):
        # $4.99 потрачено, cap $5, попытка $0.10 → 5.09 > 5 → блок
        assert can_call_paid("paid", 4.99, 5.0, 0.10) is False

    def test_paid_without_cap_blocked(self):
        # daily_cost_cap_usd=None — для безопасности блокируем
        assert can_call_paid("paid", 0, None, 0.01) is False

    def test_paid_with_zero_cap_blocked(self):
        # Cap = 0 — полная блокировка (мы используем чтобы отключить ключ)
        assert can_call_paid("paid", 0, 0.0, 0.01) is False


class TestCanCallPaidGlobalCap:
    def test_global_cap_not_set_means_no_global_check(self):
        # global_daily_cap=None → проверка глобального cap не применяется
        assert can_call_paid("paid", 0, 100.0, 0.01, global_daily_used=999.0, global_daily_cap=None) is True

    def test_global_cap_blocks_even_if_per_token_ok(self):
        # Per-token: $1 used / $5 cap, attempt $0.10 → 1.10 < 5 OK
        # Global: $9.99 used / $10 cap, attempt $0.10 → 10.09 > 10 → блок
        assert can_call_paid(
            "paid", 1.0, 5.0, 0.10,
            global_daily_used=9.99, global_daily_cap=10.0,
        ) is False

    def test_global_cap_passes_when_under(self):
        assert can_call_paid(
            "paid", 1.0, 5.0, 0.10,
            global_daily_used=5.0, global_daily_cap=10.0,
        ) is True


class TestAssertCanCallPaid:
    def test_passes_when_allowed(self):
        # Не кидает исключение
        assert_can_call_paid("paid", 1.0, 5.0, 0.10)

    def test_raises_per_token_when_token_cap_exceeded(self):
        with pytest.raises(DailyBudgetExceeded) as exc_info:
            assert_can_call_paid("paid", 4.99, 5.0, 0.10)
        assert exc_info.value.kind == "per_token"
        assert exc_info.value.limit == 5.0
        assert exc_info.value.used == 4.99
        assert exc_info.value.attempted == 0.10

    def test_raises_global_when_global_cap_exceeded(self):
        with pytest.raises(DailyBudgetExceeded) as exc_info:
            assert_can_call_paid(
                "paid", 1.0, 100.0, 0.10,
                global_daily_used=99.95, global_daily_cap=100.0,
            )
        assert exc_info.value.kind == "global"

    def test_passes_for_free_even_when_overage(self):
        # Free никогда не блокируется
        assert_can_call_paid("free", 999.0, 0.0, 999.0)

    def test_exception_message_human_readable(self):
        try:
            assert_can_call_paid("paid", 4.99, 5.0, 0.10)
        except DailyBudgetExceeded as e:
            msg = str(e)
            assert "per_token" in msg
            assert "$4.99" in msg
            assert "$5.0" in msg or "$5.00" in msg


class TestGlobalDailyCapFromEnv:
    def test_returns_none_if_not_set(self, monkeypatch):
        monkeypatch.delenv("VERA_DAILY_GLOBAL_CAP_USD", raising=False)
        assert global_daily_cap_from_env() is None

    def test_returns_none_if_empty(self, monkeypatch):
        monkeypatch.setenv("VERA_DAILY_GLOBAL_CAP_USD", "")
        assert global_daily_cap_from_env() is None

    def test_parses_valid_value(self, monkeypatch):
        monkeypatch.setenv("VERA_DAILY_GLOBAL_CAP_USD", "50.0")
        assert global_daily_cap_from_env() == 50.0

    def test_returns_none_if_invalid(self, monkeypatch):
        monkeypatch.setenv("VERA_DAILY_GLOBAL_CAP_USD", "not-a-number")
        assert global_daily_cap_from_env() is None


class TestBurnPreventionScenarios:
    """Реальные сценарии которые случились в Vera 2.0 и не должны повториться."""

    def test_scenario_25_dollar_burn_2026_06_01(self):
        """
        Ситуация: внутренний счётчик стал stale, накопилось $3.36 внутри
        но реально на Google $12.52. Cap был $7 — должен был сработать.

        В Vera 3.0: cap проверяется ДО вызова, а не после. Если cap $7,
        и каждый вызов оценивается в $0.05, то после 140 вызовов = $7
        — НЕ ДОПУСКАТЬ ни одного следующего.
        """
        token_cap = 7.0
        used = 6.99
        next_call = 0.05

        # 6.99 + 0.05 = 7.04 > 7.0 → блок
        assert can_call_paid("paid", used, token_cap, next_call) is False

    def test_scenario_global_cap_protects_from_runaway(self):
        """Даже если один токен в порядке, глобальный cap должен сработать."""
        # Per-token ok, global близко к лимиту
        assert can_call_paid(
            "paid", 1.0, 50.0, 0.10,
            global_daily_used=49.99, global_daily_cap=50.0,
        ) is False

    def test_scenario_disabled_token_via_zero_cap(self):
        """Отключение токена через cap=0 (как мы делали с id=18 demoniwwwe)."""
        assert can_call_paid("paid", 0, 0.0, 0.001) is False

    def test_scenario_token_without_cap_is_blocked(self):
        """Token без cap — не paid burn'ит. Должен быть заблокирован."""
        assert can_call_paid("paid", 0, None, 0.001) is False
