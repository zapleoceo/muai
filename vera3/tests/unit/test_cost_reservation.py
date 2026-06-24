"""Тест atomic cost reservation — закрывает TOCTOU класс ($25-burn).

Эмулируем 3 concurrent реплики которые пытаются сделать paid вызов с
лимитом $1: должно пройти ровно столько вызовов сколько укладывается под
cap, остальные должны отказать (без overshoot).
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

# fixtures should set TOKEN_SECRET before importing shared
os.environ.setdefault("TOKEN_SECRET", "test-secret")


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    """In-process SQLite. Это OK для теста логики UPDATE WHERE cap,
    SKIP LOCKED не работает в SQLite но reserve_paid_cost не использует его."""
    monkeypatch.setenv("TOKEN_SECRET", "test-secret-for-crypto-tests-only")
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test.db"

    from vera_shared.db.engine import init_engine, get_session, Base
    engine = await init_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield get_session
    from vera_shared.db.engine import close_engine
    await close_engine()


@pytest_asyncio.fixture
async def paid_token(db):
    """Создаём один paid-токен с cap=$1."""
    from vera_shared.tokens import repository as repo
    tk = await repo.upsert(
        provider="deepseek",
        label="test-eatmeat",
        plaintext_token="sk-test-1234567890",
        tier="paid",
        daily_cost_cap_usd=1.0,
    )
    return tk


@pytest.mark.asyncio
async def test_reserve_within_cap_succeeds(paid_token):
    from vera_shared.tokens import repository as repo
    ok = await repo.reserve_paid_cost(
        paid_token.id, estimated_cost=0.5, daily_cap=1.0,
    )
    assert ok is True
    refreshed = await repo.get_by_id(paid_token.id)
    assert refreshed.daily_cost_used_usd == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_reserve_over_cap_fails(paid_token):
    from vera_shared.tokens import repository as repo
    await repo.reserve_paid_cost(paid_token.id, estimated_cost=0.7, daily_cap=1.0)
    ok = await repo.reserve_paid_cost(paid_token.id, estimated_cost=0.5, daily_cap=1.0)
    # 0.7 + 0.5 = 1.2 > 1.0 → отказ
    assert ok is False
    refreshed = await repo.get_by_id(paid_token.id)
    assert refreshed.daily_cost_used_usd == pytest.approx(0.7)


@pytest.mark.asyncio
async def test_concurrent_reservations_no_overshoot(paid_token):
    """3 параллельные попытки резерва $0.5 при cap=$1.

    SQLite сериализует write — ровно 2 должны пройти. В Postgres под нагрузкой
    UPDATE WHERE cap гарантирует то же самое.
    """
    from vera_shared.tokens import repository as repo

    async def try_reserve():
        return await repo.reserve_paid_cost(
            paid_token.id, estimated_cost=0.5, daily_cap=1.0,
        )

    results = await asyncio.gather(*[try_reserve() for _ in range(5)])

    successes = sum(1 for r in results if r)
    # cap = $1, est = $0.5 → ровно 2 успеха, без overshoot
    assert successes == 2, f"expected exactly 2 reservations, got {successes}"

    refreshed = await repo.get_by_id(paid_token.id)
    assert refreshed.daily_cost_used_usd <= 1.0
    assert refreshed.daily_cost_used_usd == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_release_returns_reservation(paid_token):
    from vera_shared.tokens import repository as repo
    await repo.reserve_paid_cost(paid_token.id, estimated_cost=0.5, daily_cap=1.0)
    await repo.release_reservation(paid_token.id, reserved_cost=0.5)
    refreshed = await repo.get_by_id(paid_token.id)
    assert refreshed.daily_cost_used_usd == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_settle_paid_actual_vs_reserved(paid_token):
    from vera_shared.tokens import repository as repo
    await repo.reserve_paid_cost(paid_token.id, estimated_cost=0.5, daily_cap=1.0)
    # Реально потратили $0.3 (меньше резерва) — diff = -0.2
    await repo.record_paid_settled(
        paid_token.id, actual_cost=0.3, reserved_cost=0.5,
    )
    refreshed = await repo.get_by_id(paid_token.id)
    assert refreshed.daily_cost_used_usd == pytest.approx(0.3)
    assert refreshed.total_cost_usd == pytest.approx(0.3)
    assert refreshed.daily_used == 1


@pytest.mark.asyncio
async def test_inactive_token_reservation_rejected(paid_token):
    from vera_shared.tokens import repository as repo
    await repo.mark_inactive(paid_token.id)
    ok = await repo.reserve_paid_cost(
        paid_token.id, estimated_cost=0.1, daily_cap=1.0,
    )
    assert ok is False
