from datetime import datetime

import vera_shared.tokens.repository as repo
from vera_shared.tokens.model import TokenRecord


class TokensExhausted(Exception):
    def __init__(self, provider: str, capability: str, retry_after_seconds: int = 60) -> None:
        self.provider = provider
        self.capability = capability
        self.retry_after_seconds = retry_after_seconds
        super().__init__(
            f"No {provider} tokens available for {capability}; retry after {retry_after_seconds}s"
        )


class TokenPool:
    async def get(self, provider: str, capability: str) -> TokenRecord:
        candidates = await repo.get_by_provider_capability(provider, capability)

        available = [t for t in candidates if t.is_available()]

        if not available:
            if not candidates:
                raise TokensExhausted(provider, capability, retry_after_seconds=300)
            retry_after = min(
                t.seconds_until_available() for t in candidates if not t.is_available()
            ) or 60
            raise TokensExhausted(provider, capability, retry_after_seconds=retry_after)

        token = sorted(
            available,
            key=lambda t: t.last_used_at or datetime.min,
        )[0]

        await repo.reset_daily_if_needed(token.id)
        await repo.increment_used(token.id)
        return token

    async def on_error(
        self, token_id: int, status_code: int, retry_after_seconds: int | None = None
    ) -> None:
        # Never auto-disable a token from on_error. 401/403 cooldown 1h.
        if status_code == 429:
            cd = retry_after_seconds if retry_after_seconds and retry_after_seconds > 0 else 60
            await repo.mark_cooldown(token_id, seconds=cd)
        elif status_code >= 500:
            await repo.mark_cooldown(token_id, seconds=300)
        elif status_code in (401, 403):
            await repo.mark_cooldown(token_id, seconds=3600)
        await repo.mark_error(token_id)


_pool: TokenPool | None = None


def get_pool() -> TokenPool:
    global _pool
    if _pool is None:
        _pool = TokenPool()
    return _pool
