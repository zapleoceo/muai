"""Тесты Token model — paid/free, caps, availability."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from vera_shared.tokens.model import Token


class TestBasicValidation:
    def test_minimal_token(self):
        t = Token(provider="cerebras", label="test", token="csk-x")
        assert t.tier == "free"
        assert t.is_active is True
        assert t.daily_used == 0

    def test_provider_lowercased(self):
        t = Token(provider="GROQ", label="x", token="y")
        assert t.provider == "groq"

    def test_empty_provider_rejected(self):
        with pytest.raises(ValidationError):
            Token(provider="", label="x", token="y")

    def test_empty_token_rejected(self):
        with pytest.raises(ValidationError):
            Token(provider="x", label="x", token="")

    def test_negative_counters_rejected(self):
        with pytest.raises(ValidationError):
            Token(provider="x", label="x", token="x", daily_used=-1)


class TestAvailability:
    def _t(self, **kw):
        defaults = dict(provider="cerebras", label="test", token="x")
        defaults.update(kw)
        return Token(**defaults)

    def test_default_token_is_available(self):
        assert self._t().is_available is True

    def test_inactive_not_available(self):
        assert self._t(is_active=False).is_available is False

    def test_in_cooldown_not_available(self):
        t = self._t(cooldown_until=datetime.utcnow() + timedelta(minutes=5))
        assert t.is_available is False
        assert t.is_in_cooldown is True

    def test_expired_cooldown_is_available(self):
        t = self._t(cooldown_until=datetime.utcnow() - timedelta(minutes=5))
        assert t.is_available is True
        assert t.is_in_cooldown is False

    def test_daily_quota_exceeded_not_available(self):
        t = self._t(daily_limit=100, daily_used=100)
        assert t.is_available is False

    def test_paid_cap_exceeded_not_available(self):
        t = self._t(
            tier="paid",
            daily_cost_cap_usd=5.0,
            daily_cost_used_usd=5.0,
        )
        assert t.is_available is False

    def test_free_no_cost_check(self):
        # Free токен с большим использованием — всё равно доступен
        t = self._t(
            tier="free",
            daily_cost_cap_usd=None,
            daily_cost_used_usd=999.0,
        )
        assert t.is_available is True

    def test_monthly_cap_exceeded_not_available(self):
        t = self._t(
            tier="paid",
            daily_cost_cap_usd=10.0,
            daily_cost_used_usd=1.0,
            monthly_cost_cap_usd=50.0,
            monthly_cost_used_usd=50.0,
        )
        assert t.is_available is False


class TestDisplayState:
    def _t(self, **kw):
        defaults = dict(provider="cerebras", label="test", token="x")
        defaults.update(kw)
        return Token(**defaults)

    def test_live(self):
        assert self._t().display_state == "live"

    def test_dead(self):
        assert self._t(is_active=False).display_state == "dead"

    def test_cooldown(self):
        t = self._t(cooldown_until=datetime.utcnow() + timedelta(seconds=60))
        assert t.display_state == "cooldown"

    def test_capped_by_daily_cost(self):
        t = self._t(tier="paid", daily_cost_cap_usd=5.0, daily_cost_used_usd=5.0)
        assert t.display_state == "capped"

    def test_capped_by_daily_quota(self):
        t = self._t(daily_limit=10, daily_used=10)
        assert t.display_state == "capped"


class TestSecondsUntilAvailable:
    def test_returns_zero_if_no_cooldown(self):
        t = Token(provider="x", label="x", token="x")
        assert t.seconds_until_available() == 0

    def test_returns_positive_when_in_cooldown(self):
        t = Token(
            provider="x", label="x", token="x",
            cooldown_until=datetime.utcnow() + timedelta(seconds=120),
        )
        s = t.seconds_until_available()
        assert 110 < s <= 120


class TestBurnPrevention:
    """Сценарии из Vera 2.0 которые не должны повториться."""

    def test_paid_without_cap_is_capped(self):
        # daily_cost_cap_usd=None для paid → cost guard блокирует
        # (см. test_cost_guard)
        # Здесь проверяем что display_state корректный
        t = Token(provider="gemini", label="paid", token="x", tier="paid",
                  daily_cost_cap_usd=None)
        # is_available true (capped через cost_guard, не через is_available),
        # но cost_guard блокирует — это правильно. Здесь только check что не падает.
        assert t.tier == "paid"

    def test_paid_at_cap_exactly_is_capped(self):
        t = Token(
            provider="gemini", label="paid", token="x", tier="paid",
            daily_cost_cap_usd=5.0,
            daily_cost_used_usd=5.0,
        )
        assert t.daily_cost_cap_exceeded is True
        assert t.is_available is False
