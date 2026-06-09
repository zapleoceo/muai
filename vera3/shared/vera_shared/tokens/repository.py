"""Token repository — CRUD + ротация + reset_daily.

Используется LLM client'ом для выбора следующего токена.

КРИТИЧНО: `reserve_paid_cost` атомарен — UPDATE с условием cap.
Это закрывает TOCTOU класс багов ($25-burn инцидент).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Iterable

from sqlalchemy import select, text, update
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
            if capabilities is not None:
                existing.capabilities = capabilities
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


async def reserve_paid_cost(
    token_id: int,
    *,
    estimated_cost: float,
    daily_cap: float,
    monthly_cap: float | None = None,
) -> bool:
    """Атомарно зарезервировать estimated_cost в счётчиках токена.

    Закрывает TOCTOU. Возвращает True если резерв удался, False если cap превышен.
    Если резерв удался — после реального вызова делаем `settle_paid_cost(diff)`.
    Если упал — пробуем следующий токен.

    SQL: UPDATE ... WHERE cost_used + est <= cap. Если 0 rows — отказ.
    """
    if estimated_cost <= 0:
        # Halacha: для нулевых затрат cap НЕ резервируем, чтобы LRU работал
        return True
    async with get_session() as s:
        # Условие = "ещё помещается под cap И активен"
        conds = ["id = :tid", "is_active = TRUE",
                 "(daily_cost_used_usd + :est) <= :daily_cap"]
        params: dict = {"tid": token_id, "est": estimated_cost, "daily_cap": daily_cap}
        if monthly_cap is not None:
            conds.append("(monthly_cost_used_usd + :est) <= :monthly_cap")
            params["monthly_cap"] = monthly_cap
        sql = (
            "UPDATE tokens SET "
            "  daily_cost_used_usd = daily_cost_used_usd + :est, "
            "  monthly_cost_used_usd = monthly_cost_used_usd + :est "
            f"WHERE {' AND '.join(conds)}"
        )
        result = await s.execute(text(sql), params)
        return (result.rowcount or 0) > 0


async def settle_paid_cost(token_id: int, *, diff_usd: float) -> None:
    """Скорректировать счётчики на diff = actual - estimated после успешного вызова.

    diff может быть положительным (мы съели больше чем зарезервировали)
    или отрицательным (вернули остаток).
    """
    if abs(diff_usd) < 1e-9:
        return
    async with get_session() as s:
        await s.execute(text(
            "UPDATE tokens SET "
            "  daily_cost_used_usd = GREATEST(0, daily_cost_used_usd + :d), "
            "  monthly_cost_used_usd = GREATEST(0, monthly_cost_used_usd + :d), "
            "  total_cost_usd = GREATEST(0, total_cost_usd + :d) "
            "WHERE id = :tid"
        ), {"tid": token_id, "d": diff_usd})


async def record_usage(
    token_id: int,
    *,
    tokens_in: int,
    tokens_out: int,
    cost_usd: float,
) -> None:
    """Атомарно увеличить счётчики после успешного вызова.

    ⚠️ Для PAID вызовов используется `reserve_paid_cost` + `settle_paid_cost`.
    Этот метод — для free/trial где cap не применяется, а также для
    инкремента daily_used / total_cost_usd / last_used_at.
    """
    async with get_session() as s:
        await s.execute(
            update(TokenRow)
            .where(TokenRow.id == token_id)
            .values(
                daily_used=TokenRow.daily_used + 1,
                # Эти три уже учтены в reserve_paid_cost для paid токенов —
                # для free/trial cost_usd обычно 0, так что без удвоения.
                daily_cost_used_usd=TokenRow.daily_cost_used_usd + cost_usd,
                monthly_cost_used_usd=TokenRow.monthly_cost_used_usd + cost_usd,
                total_cost_usd=TokenRow.total_cost_usd + cost_usd,
                last_used_at=datetime.utcnow(),
            )
        )


async def record_free_usage(token_id: int) -> None:
    """Для free/trial вызовов — только инкрементим daily_used + last_used_at."""
    async with get_session() as s:
        await s.execute(
            update(TokenRow)
            .where(TokenRow.id == token_id)
            .values(
                daily_used=TokenRow.daily_used + 1,
                last_used_at=datetime.utcnow(),
            )
        )


async def record_paid_settled(
    token_id: int,
    *,
    actual_cost: float,
    reserved_cost: float,
) -> None:
    """После успешного paid вызова: diff = actual - reserved, инкремент
    daily_used + total_cost_usd, last_used_at. Cost-counters уже у нас в reserve.
    """
    diff = actual_cost - reserved_cost
    async with get_session() as s:
        await s.execute(text(
            "UPDATE tokens SET "
            "  daily_used = daily_used + 1, "
            "  total_cost_usd = total_cost_usd + :actual, "
            "  daily_cost_used_usd = GREATEST(0, daily_cost_used_usd + :diff), "
            "  monthly_cost_used_usd = GREATEST(0, monthly_cost_used_usd + :diff), "
            "  last_used_at = NOW() "
            "WHERE id = :tid"
        ), {"tid": token_id, "actual": actual_cost, "diff": diff})


async def release_reservation(token_id: int, *, reserved_cost: float) -> None:
    """Если paid вызов упал — освобождаем зарезервированный cost."""
    if reserved_cost <= 0:
        return
    async with get_session() as s:
        await s.execute(text(
            "UPDATE tokens SET "
            "  daily_cost_used_usd = GREATEST(0, daily_cost_used_usd - :est), "
            "  monthly_cost_used_usd = GREATEST(0, monthly_cost_used_usd - :est) "
            "WHERE id = :tid"
        ), {"tid": token_id, "est": reserved_cost})


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


async def reset_monthly() -> int:
    """Сбросить ежемесячные cost счётчики. Запускать 1-го числа каждого месяца."""
    async with get_session() as s:
        result = await s.execute(update(TokenRow).values(
            monthly_cost_used_usd=0.0,
        ))
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
