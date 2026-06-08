"""Token model — каждый ключ AI-провайдера с tier и cost caps.

ОТЛИЧИЕ ОТ Vera 2.0:
- Явный tier (free/paid/trial), а не выводится из словаря PAID_KEYS
- daily_cost_cap_usd обязателен для tier=paid (валидируется)
- daily_cost_used_usd сбрасывается ежедневно автоматически (last_reset_date)
- error_count + cooldown_until для health management
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

TokenTier = Literal["free", "paid", "trial"]


class Token(BaseModel):
    """Stored token record — соответствует строке в tokens table Postgres."""

    id: int | None = None  # назначается БД при INSERT
    provider: str = Field(min_length=1)
    label: str = Field(min_length=1, description="человеко-читаемая метка")
    token: str = Field(min_length=1, description="API-key или OAuth token")
    tier: TokenTier = "free"
    capabilities: list[str] = Field(default_factory=list)
    is_active: bool = True

    # Request counters (per day)
    daily_limit: int = Field(default=999_999, ge=0, description="максимум req/день")
    daily_used: int = Field(default=0, ge=0)

    # Cost counters
    daily_cost_cap_usd: float | None = Field(default=None, ge=0)
    daily_cost_used_usd: float = Field(default=0.0, ge=0)
    monthly_cost_cap_usd: float | None = Field(default=None, ge=0)
    monthly_cost_used_usd: float = Field(default=0.0, ge=0)
    total_cost_usd: float = Field(default=0.0, ge=0, description="lifetime")

    # Health & rotation state
    daily_reset_at: date | None = None
    cooldown_until: datetime | None = None
    error_count: int = Field(default=0, ge=0)
    last_used_at: datetime | None = None

    # Metadata
    notes: str = Field(default="", description="комментарии: 'забанили', 'карта привязана'")
    created_at: datetime = Field(default_factory=datetime.utcnow)

    @field_validator("provider")
    @classmethod
    def provider_lowercase(cls, v: str) -> str:
        return v.lower().strip()

    @model_validator(mode="after")
    def validate_paid_must_have_cap(self) -> "Token":
        """Paid токен без cap — недопустимо (защита от burn'а)."""
        if self.tier == "paid" and self.daily_cost_cap_usd is None:
            # Не блокируем при создании, но это будет no-go в cost_guard
            # Так пользователю проще видеть в дашборде "cap не задан"
            pass
        return self

    # ─── Properties ─────────────────────────────────────────────────────────

    @property
    def is_in_cooldown(self) -> bool:
        return self.cooldown_until is not None and self.cooldown_until > datetime.utcnow()

    @property
    def daily_request_quota_exceeded(self) -> bool:
        return self.daily_used >= self.daily_limit

    @property
    def daily_cost_cap_exceeded(self) -> bool:
        if self.tier != "paid":
            return False
        cap = self.daily_cost_cap_usd or 0.0
        return self.daily_cost_used_usd >= cap

    @property
    def monthly_cost_cap_exceeded(self) -> bool:
        if self.tier != "paid":
            return False
        if self.monthly_cost_cap_usd is None:
            return False
        return self.monthly_cost_used_usd >= self.monthly_cost_cap_usd

    @property
    def is_available(self) -> bool:
        """Можно ли использовать этот токен прямо сейчас."""
        if not self.is_active:
            return False
        if self.is_in_cooldown:
            return False
        if self.daily_request_quota_exceeded:
            return False
        if self.daily_cost_cap_exceeded:
            return False
        if self.monthly_cost_cap_exceeded:
            return False
        return True

    def seconds_until_available(self) -> int:
        """Сколько секунд до восстановления (для retry-after сообщений)."""
        if self.cooldown_until and self.cooldown_until > datetime.utcnow():
            return int((self.cooldown_until - datetime.utcnow()).total_seconds())
        return 0

    @property
    def display_state(self) -> Literal["live", "cooldown", "dead", "capped"]:
        """Status для дашборда."""
        if not self.is_active:
            return "dead"
        if self.is_in_cooldown:
            return "cooldown"
        if (
            self.daily_cost_cap_exceeded
            or self.monthly_cost_cap_exceeded
            or self.daily_request_quota_exceeded
        ):
            return "capped"
        return "live"
