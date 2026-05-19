from datetime import date, datetime, timedelta

from sqlalchemy import select, update

from vera_shared.db.engine import get_session
from vera_shared.db.models import Token
from vera_shared.tokens.model import PROVIDER_DEFAULT_CAPS, TokenRecord


def _to_record(row: Token) -> TokenRecord:
    caps = row.capabilities or PROVIDER_DEFAULT_CAPS.get(row.provider, [])
    return TokenRecord(
        id=row.id, provider=row.provider, label=row.label, token=row.token,
        capabilities=caps, is_active=row.is_active, daily_limit=row.daily_limit,
        daily_used=row.daily_used, daily_reset_at=row.daily_reset_at,
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
            row.tokens_in = (row.tokens_in or 0) + max(0, tokens_in)
            row.tokens_out = (row.tokens_out or 0) + max(0, tokens_out)
            row.cost_usd = (row.cost_usd or 0.0) + max(0.0, cost)
            await session.commit()


async def reset_daily_if_needed(token_id: int) -> None:
    today = date.today()
    async with get_session() as session:
        row = await session.get(Token, token_id)
        if row and (row.daily_reset_at is None or row.daily_reset_at < today):
            row.daily_used = 0
            row.daily_reset_at = today
            await session.commit()


async def upsert(
    provider: str, label: str, token: str, capabilities: list[str]
) -> TokenRecord:
    async with get_session() as session:
        result = await session.execute(
            select(Token).where(Token.provider == provider, Token.label == label)
        )
        row = result.scalar_one_or_none()
        if row is None:
            row = Token(provider=provider, label=label, token=token, capabilities=capabilities)
            session.add(row)
        else:
            row.token = token
            row.capabilities = capabilities
        await session.commit()
        await session.refresh(row)
        return _to_record(row)
