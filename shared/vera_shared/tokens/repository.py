import os
from datetime import date, datetime, timedelta

from sqlalchemy import select, update

from vera_shared.crypto import decrypt, encrypt, is_encrypted
from vera_shared.db.engine import get_session
from vera_shared.db.models import Token
from vera_shared.tokens.model import PROVIDER_DEFAULT_CAPS, TokenRecord


def _master() -> str:
    s = os.environ.get("SESSION_SECRET") or os.environ.get("TOKEN_ENC_KEY")
    if not s:
        raise RuntimeError("SESSION_SECRET (or TOKEN_ENC_KEY) is not set — refuse to handle tokens")
    return s


def _to_record(row: Token) -> TokenRecord:
    caps = row.capabilities or PROVIDER_DEFAULT_CAPS.get(row.provider, [])
    plain_token = decrypt(row.token, _master()) if is_encrypted(row.token) else row.token
    return TokenRecord(
        id=row.id, provider=row.provider, label=row.label, token=plain_token,
        capabilities=caps, is_active=row.is_active, daily_limit=row.daily_limit,
        daily_used=row.daily_used,
        daily_cost_limit_usd=getattr(row, "daily_cost_limit_usd", None),
        daily_cost_used_usd=getattr(row, "daily_cost_used_usd", 0.0) or 0.0,
        daily_reset_at=row.daily_reset_at,
        cooldown_until=row.cooldown_until, error_count=row.error_count,
        last_used_at=row.last_used_at, created_at=row.created_at,
    )


async def get_all_active() -> list[TokenRecord]:
    async with get_session() as session:
        result = await session.execute(select(Token).where(Token.is_active == True))
        return [_to_record(r) for r in result.scalars().all()]


async def get_by_capability(cap: str) -> list[TokenRecord]:
    return [r for r in await get_all_active() if cap in r.capabilities]


async def get_by_provider_capability(provider: str, cap: str) -> list[TokenRecord]:
    return [
        r for r in await get_all_active()
        if r.provider == provider and cap in r.capabilities
    ]


async def mark_cooldown(token_id: int, seconds: int) -> None:
    until = datetime.utcnow() + timedelta(seconds=seconds)
    async with get_session() as session:
        await session.execute(
            update(Token).where(Token.id == token_id).values(cooldown_until=until)
        )
        await session.commit()


async def mark_error(token_id: int) -> None:
    async with get_session() as session:
        row = await session.get(Token, token_id)
        if row:
            row.error_count = (row.error_count or 0) + 1
            await session.commit()


async def mark_inactive(token_id: int) -> None:
    async with get_session() as session:
        await session.execute(
            update(Token).where(Token.id == token_id).values(is_active=False)
        )
        await session.commit()


async def increment_used(token_id: int) -> None:
    async with get_session() as session:
        row = await session.get(Token, token_id)
        if row:
            row.daily_used = (row.daily_used or 0) + 1
            row.last_used_at = datetime.utcnow()
            await session.commit()


async def record_usage(token_id: int, tokens_in: int, tokens_out: int, cost: float) -> None:
    async with get_session() as session:
        row = await session.get(Token, token_id)
        if row:
            c = max(0.0, cost)
            row.tokens_in = (row.tokens_in or 0) + max(0, tokens_in)
            row.tokens_out = (row.tokens_out or 0) + max(0, tokens_out)
            row.cost_usd = (row.cost_usd or 0.0) + c
            # Per-key rolling daily cost counter (resets in reset_daily_if_needed)
            row.daily_cost_used_usd = (row.daily_cost_used_usd or 0.0) + c
            await session.commit()


async def reset_daily_if_needed(token_id: int) -> None:
    today = date.today()
    async with get_session() as session:
        row = await session.get(Token, token_id)
        if row and (row.daily_reset_at is None or row.daily_reset_at < today):
            row.daily_used = 0
            row.daily_cost_used_usd = 0.0
            row.daily_reset_at = today
            await session.commit()


async def set_cost_limit(token_id: int, limit_usd: float | None) -> bool:
    """Set or remove the per-key daily cost cap. Pass None to remove."""
    async with get_session() as session:
        row = await session.get(Token, token_id)
        if row is None:
            return False
        row.daily_cost_limit_usd = limit_usd
        await session.commit()
        return True


async def upsert(
    provider: str, label: str, token: str, capabilities: list[str]
) -> TokenRecord:
    encrypted = encrypt(token, _master())
    async with get_session() as session:
        result = await session.execute(
            select(Token).where(Token.provider == provider, Token.label == label)
        )
        row = result.scalar_one_or_none()
        if row is None:
            row = Token(provider=provider, label=label, token=encrypted, capabilities=capabilities)
            session.add(row)
        else:
            row.token = encrypted
            row.capabilities = capabilities
        await session.commit()
        await session.refresh(row)
        return _to_record(row)


async def migrate_plaintext_tokens() -> int:
    """Encrypt any tokens still in plaintext. Idempotent."""
    master = _master()
    migrated = 0
    async with get_session() as session:
        result = await session.execute(select(Token))
        for row in result.scalars().all():
            if row.token and not is_encrypted(row.token):
                row.token = encrypt(row.token, master)
                migrated += 1
        if migrated:
            await session.commit()
    return migrated
