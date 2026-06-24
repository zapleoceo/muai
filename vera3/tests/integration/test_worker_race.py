"""Integration test: 3 параллельные реплики brain-triage берут pending events
через SELECT FOR UPDATE SKIP LOCKED, и КАЖДОЕ событие обрабатывается ровно раз.

Это основной scaling-claim Веры. Без этого теста — недоказан.

Запуск требует ЖИВУЮ Postgres (не SQLite — SKIP LOCKED postgres-specific).
Локально: docker run -p 5433:5432 -e POSTGRES_PASSWORD=test pgvector/pgvector:pg16
Или укажи TEST_DATABASE_URL.
"""
from __future__ import annotations

import asyncio
import os

import pytest
import pytest_asyncio

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://vera:test@localhost:5433/vera_test",
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_INTEGRATION_TESTS"),
    reason="Set RUN_INTEGRATION_TESTS=1 + provide TEST_DATABASE_URL",
)


@pytest_asyncio.fixture
async def pg_db(monkeypatch):
    monkeypatch.setenv("TOKEN_SECRET", "test-secret-for-integration")
    monkeypatch.setenv("DATABASE_URL", TEST_DB_URL)
    from vera_shared.db.engine import init_engine, close_engine, get_session, Base
    engine = await init_engine(TEST_DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield get_session
    await close_engine()


@pytest.mark.asyncio
async def test_three_workers_no_duplicate_claim(pg_db):
    """100 pending events + 3 concurrent claimers. Каждое событие захвачено
    ровно одним claimer'ом — нет дублей."""
    from datetime import datetime
    from sqlalchemy import text
    from vera_shared.db.engine import get_session
    from vera_shared.db.models import EventRow

    # Seed 100 pending events
    async with get_session() as s:
        for i in range(100):
            s.add(EventRow(
                source="test",
                source_event_id=f"e{i}",
                content_text=f"text {i}",
                occurred_at=datetime.utcnow(),
                triage_status="pending",
            ))

    # Имитируем 3 воркера. Каждый делает ~5 claim'ов по 10 событий = 30 batches × 10 = 300+
    # Хватит чтобы захватить все 100 с margin.
    async def claim_batches() -> list[int]:
        """Воркер собирает свои id."""
        my_ids: list[int] = []
        for _ in range(20):
            async with get_session() as s:
                rs = await s.execute(text("""
                    UPDATE events
                    SET triage_status='processing', triage_started_at=NOW()
                    WHERE id IN (
                      SELECT id FROM events
                      WHERE triage_status='pending'
                      ORDER BY id
                      LIMIT 10
                      FOR UPDATE SKIP LOCKED
                    )
                    RETURNING id
                """))
                claimed = list(rs.scalars().all())
            if not claimed:
                break
            my_ids.extend(claimed)
        return my_ids

    results = await asyncio.gather(claim_batches(), claim_batches(), claim_batches())

    all_claimed = [eid for batch in results for eid in batch]
    # ВСЕ 100 захвачены
    assert len(all_claimed) == 100
    # НИ ОДНО событие не захвачено дважды
    assert len(set(all_claimed)) == 100, f"duplicate claims: {len(all_claimed) - len(set(all_claimed))}"

    # Проверяем что в БД все 100 в processing
    async with get_session() as s:
        rs = await s.execute(text(
            "SELECT triage_status, COUNT(*) FROM events GROUP BY 1"
        ))
        counts = dict(rs.all())
    assert counts.get("processing", 0) == 100
    assert counts.get("pending", 0) == 0


@pytest.mark.asyncio
async def test_watchdog_returns_stuck_processing(pg_db):
    """processing старше STUCK_AFTER_S → watchdog возвращает в pending."""
    from datetime import datetime, timedelta
    from sqlalchemy import text
    from vera_shared.db.engine import get_session
    from vera_shared.db.models import EventRow

    async with get_session() as s:
        # Событие в processing с triage_started_at 15 минут назад
        s.add(EventRow(
            source="test", source_event_id="stuck1",
            content_text="x", occurred_at=datetime.utcnow(),
            triage_status="processing",
            triage_started_at=datetime.utcnow() - timedelta(minutes=15),
        ))
        # И одно свежее (не trigger'ит watchdog)
        s.add(EventRow(
            source="test", source_event_id="fresh1",
            content_text="x", occurred_at=datetime.utcnow(),
            triage_status="processing",
            triage_started_at=datetime.utcnow(),
        ))

    # watchdog: STUCK_AFTER_S=600
    async with get_session() as s:
        rs = await s.execute(text(
            "UPDATE events SET triage_status='pending', triage_started_at=NULL "
            "WHERE triage_status='processing' "
            "  AND triage_started_at < NOW() - INTERVAL '600 seconds' "
            "RETURNING source_event_id"
        ))
        revived = list(rs.scalars().all())

    assert revived == ["stuck1"]
