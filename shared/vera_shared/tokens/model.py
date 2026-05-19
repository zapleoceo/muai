from dataclasses import dataclass, field
from datetime import date, datetime


PROVIDER_DEFAULT_CAPS: dict[str, list[str]] = {
    "gemini": ["chat:fast", "prefilter"],
    "deepseek": ["chat:smart", "chat:code"],
    "voyage": ["embed"],
    "anthropic": ["chat:smart", "chat:code"],
}


@dataclass
class TokenRecord:
    id: int
    provider: str
    label: str
    token: str
    capabilities: list[str]
    is_active: bool = True
    daily_limit: int = 1500
    daily_used: int = 0
    daily_reset_at: date | None = None
    cooldown_until: datetime | None = None
    error_count: int = 0
    last_used_at: datetime | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)

    def is_available(self) -> bool:
        if not self.is_active:
            return False
        if self.cooldown_until and self.cooldown_until > datetime.utcnow():
            return False
        if self.daily_used >= self.daily_limit:
            return False
        return True

    def seconds_until_available(self) -> int:
        if self.cooldown_until and self.cooldown_until > datetime.utcnow():
            return int((self.cooldown_until - datetime.utcnow()).total_seconds())
        return 0
