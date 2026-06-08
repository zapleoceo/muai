"""Token repository — CRUD + ротация + reset_daily.

Используется LLM client'ом для выбора следующего токена.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Iterable

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.engine import get_session
from vera_shared.db.models import TokenRow
from vera_shared.tokens.crypto import decrypt, encrypt
from vera_shared.tokens.model import Token, TokenTier


# ─── CRUD ───────────────────────────────────────────────────────────────────


async def upsert(
    *,
    provider: str,
    label: str,
    plaintext_token: str,
    tier: TokenTier = "free",
    capabilities: list[str] | None = None,
    daily_cost_cap_usd: float | None = None,
    monthly_cost_cap_usd: float | None = None,
    notes: str = "",
) -> Token:
    """Insert or update token by (provider, label)."""
    encrypted = encrypt(plaintext_token)
    async with get_session() as s:
        existing = await _find_by_provider_label(s, provider, label)
        if existing:
            existing.token_encrypted = encrypted
            existing.tier = tier
            existing.capabilities = capabilities or existing.capabilities
            existing.daily_cost_cap_usd = daily_cost_cap_usd
            existing.monthly_cost_cap_usd = monthly_cost_cap_usd
            existing.notes = notes
            existing.is_active = True
            row = existing
        else:
            row = TokenRow(
                provider=provider.lower().strip(),
                label=label,
                token_encrypted=encrypted,
                tier=tier,
                capabilities=capabilities or [],
                daily_cost_cap_usd=daily_cost_cap_usd,
                monthly_cost_cap_usd=monthly_cost_cap_usd,
                notes=notes,
            )
            s.add(row)
        await s.flush()
        return _row_to_model(row)


async def delete(token_id: int) -> None:
    async with get_session() as s:
        row = await s.get(TokenRow, token_id)
        if row:
            await s.delete(row)


async def get_by_id(token_id: int) -> Token | None:
    async with get_session() as s:
        row = await s.get(TokenRow, token_id)
        return _row_to_model(row) if row else None


async def list_all(*, active_only: bool = False) -> list[Token]:
    async with get_session() as s:
        q = select(TokenRow).order_by(TokenRow.provider, TokenRow.id)
        if active_only:
            q = q.where(TokenRow.is_active.is_(True))
        rows = (await s.execute(q)).scalars().all()
        return [_row_to_model(r) for r in rows]


async def list_for_provider(provider: str) -> list[Token]:
    async with get_session() as s:
        q = (
            select(TokenRow)
            .where(TokenRow.provider == provider.lower())
            .order_by(TokenRow.id)
        )
        return [_row_to_model(r) for r in (await s.execute(q)).scalars().all()]


# ─── Ротация ────────────────────────────────────────────────────────────────


async def pick_available(
    provider: str,
    *,
    require_tier: TokenTier | None = None,
) -> Token | None:
    """Выбрать самый давно-использованный available токен провайдера.

    LRU-strategy: round-robin между ключами.
    """
    candidates = await list_for_provider(provider)
    pool = [t for t in candidates if t.is_available]
    if require_tier:
        pool = [t for t in pool if t.tier == require_tier]
    if not pool:
        return None
    # LRU: тот у кого last_used_at самый старый (или None)
    pool.sort(key=lambda t: t.last_used_at or datetime.min)
    return pool[0]


# ─── Atomic increment операции ──────────────────────────────────────────────


async def record_usage(
    token_id: int,
    *,
    tokens_in: int,
    tokens_out: int,
    cost_usd: float,
) -> None:
    """Атомарно увеличить счётчики после успешного вызова."""
    async with get_session() as s:
        await s.execute(
            update(TokenRow)
            .where(TokenRow.id == token_id)
            .values(
                daily_used=TokenRow.daily_used + 1,
                daily_cost_used_usd=TokenRow.daily_cost_used_usd + cost_usd,
                monthly_cost_used_usd=TokenRow.monthly_cost_used_usd + cost_usd,
                total_cost_usd=TokenRow.total_cost_usd + cost_usd,
                last_used_at=datetime.utcnow(),
            )
        )


async def mark_cooldown(token_id: int, *, seconds: int) -> None:
    async with get_session() as s:
        await s.execute(
            update(TokenRow)
            .where(TokenRow.id == token_id)
            .values(
                cooldown_until=datetime.utcnow() + timedelta(seconds=seconds),
                error_count=TokenRow.error_count + 1,
            )
        )


async def mark_inactive(token_id: int, *, reason: str = "") -> None:
    async with get_session() as s:
        await s.execute(
            update(TokenRow)
            .where(TokenRow.id == token_id)
            .values(is_active=False, notes=reason or "auto-disabled")
        )


async def reset_daily(token_id: int | None = None) -> int:
    """Сбросить дневные счётчики. Если token_id=None — все где daily_reset_at стар."""
    today = date.today()
    async with get_session() as s:
        q = update(TokenRow).values(
            daily_used=0,
            daily_cost_used_usd=0.0,
            daily_reset_at=today,
            error_count=0,
        )
        if token_id is not None:
            q = q.where(TokenRow.id == token_id)
        else:
            q = q.where(
                (TokenRow.daily_reset_at.is_(None))
                | (TokenRow.daily_reset_at < today)
            )
        result = await s.execute(q)
        return result.rowcount or 0


async def set_cost_cap(
    token_id: int,
    *,
    daily_cap_usd: float | None = None,
    monthly_cap_usd: float | None = None,
) -> None:
    async with get_session() as s:
        values: dict = {}
        if daily_cap_usd is not None:
            values["daily_cost_cap_usd"] = daily_cap_usd
        if monthly_cap_usd is not None:
            values["monthly_cost_cap_usd"] = monthly_cap_usd
        if values:
            await s.execute(
                update(TokenRow).where(TokenRow.id == token_id).values(**values)
            )


# ─── Helpers ────────────────────────────────────────────────────────────────


async def _find_by_provider_label(s: AsyncSession, provider: str, label: str) -> TokenRow | None:
    q = select(TokenRow).where(
        TokenRow.provider == provider.lower(),
        TokenRow.label == label,
    )
    return (await s.execute(q)).scalar_one_or_none()


def _row_to_model(row: TokenRow) -> Token:
    """ORM row → Pydantic Token (с расшифрованным токеном)."""
    return Token(
        id=row.id,
        provider=row.provider,
        label=row.label,
        token=decrypt(row.token_encrypted),
        tier=row.tier,  # type: ignore[arg-type]
        capabilities=row.capabilities,
        is_active=row.is_active,
        daily_limit=row.daily_limit,
        daily_used=row.daily_used,
        daily_cost_cap_usd=row.daily_cost_cap_usd,
        daily_cost_used_usd=row.daily_cost_used_usd,
        monthly_cost_cap_usd=row.monthly_cost_cap_usd,
        monthly_cost_used_usd=row.monthly_cost_used_usd,
        total_cost_usd=row.total_cost_usd,
        daily_reset_at=row.daily_reset_at,
        cooldown_until=row.cooldown_until,
        error_count=row.error_count,
        last_used_at=row.last_used_at,
        notes=row.notes,
        created_at=row.created_at,
    )
