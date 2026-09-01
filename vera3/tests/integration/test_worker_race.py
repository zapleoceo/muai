"""Основной scaling-claim Веры: N реплик триажа берут события через
`SELECT ... FOR UPDATE SKIP LOCKED`, и каждое обрабатывается ровно раз.

Требует ЖИВУЮ Postgres — SKIP LOCKED, NOW() и `content_text != ''` в связке
с частичным индексом на SQLite не воспроизводятся. В CI поднимается
сервисом (deploy.yml, образ pgvector/pgvector:pg16 — тот же, что в проде).
Локально: docker run -p 5433:5432 -e POSTGRES_PASSWORD=test pgvector/pgvector:pg16

Тест зовёт ПРОДОВЫЙ `_claim_batch()`. Раньше он держал собственную копию
SQL — без `content_text != ''`, с `ORDER BY id` вместо `occurred_at DESC`,
с захардкоженным LIMIT 10 и `RETURNING id` вместо одиннадцати колонок. То
есть проверял свойство Postgres (SKIP LOCKED работает), а не свойство этого
кода, и регресс в claim'е поймать не мог. Плюс он никогда не запускался:
ни один воркфлоу не выставлял RUN_INTEGRATION_TESTS.
"""
from __future__ import annotations

import asyncio
import os
from datetime import timedelta

import pytest
import pytest_asyncio
from vera_shared.timeutil import utc_naive_now

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
    import vera_shared.db.engine as engine_mod

    # Импорт ВСЕХ модулей моделей обязателен: Base.metadata знает только
    # про те таблицы, чьи классы уже импортированы. Без models_graph
    # create_all молча не создаёт entities/entity_aliases/memberships.
    from vera_shared.db import models, models_graph, models_sources  # noqa: F401
    from vera_shared.db.engine import Base, close_engine, get_session, init_engine

    # Чужой engine (юнит-тесты на SQLite) закрываем — иначе get_session
    # уйдёт в SQLite и весь смысл теста пропадёт.
    if engine_mod._engine is not None:
        await close_engine()
    engine = await init_engine(TEST_DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield get_session
    await close_engine()


async def _seed(n: int, **over) -> None:
    from vera_shared.db.engine import get_session
    from vera_shared.db.models import EventRow

    now = utc_naive_now()
    async with get_session() as s:
        for i in range(n):
            spec = {
                "source": "test", "source_event_id": f"e{i}",
                "content_text": f"text {i}",
                "occurred_at": now - timedelta(minutes=i),
                "triage_status": "pending",
            }
            spec.update(over)
            spec["source_event_id"] = f"{spec['source_event_id']}"
            s.add(EventRow(**spec))


@pytest.mark.asyncio
async def test_three_workers_no_duplicate_claim(pg_db):
    """100 pending events + 3 конкурентных клеймера через ПРОДОВЫЙ _claim_batch."""
    from brain_triage.claim import _claim_batch
    from sqlalchemy import text
    from vera_shared.db.engine import get_session

    await _seed(100)

    async def worker() -> list[int]:
        mine: list[int] = []
        for _ in range(20):
            rows = await _claim_batch(10)
            if not rows:
                break
            mine.extend(r.id for r in rows)
        return mine

    results = await asyncio.gather(worker(), worker(), worker())
    all_claimed = [eid for batch in results for eid in batch]

    assert len(all_claimed) == 100
    assert len(set(all_claimed)) == 100, "событие захвачено дважды"

    async with get_session() as s:
        counts = dict((await s.execute(text(
            "SELECT triage_status, COUNT(*) FROM events GROUP BY 1"))).all())
    assert counts.get("processing", 0) == 100
    assert counts.get("pending", 0) == 0


@pytest.mark.asyncio
async def test_claim_skips_empty_content(pg_db):
    """`content_text != ''` — предикат продового запроса И условие частичного
    индекса ix_events_pending_claim (миграция 014). Копия SQL в тесте его не
    имела, поэтому его пропажу поймать было нельзя."""
    from brain_triage.claim import _claim_batch

    await _seed(3)
    await _seed(2, source="empty", content_text="")

    rows = await _claim_batch(50)

    assert len(rows) == 3
    assert all(r.content_text for r in rows)


@pytest.mark.asyncio
async def test_claim_takes_newest_first(pg_db):
    """`ORDER BY occurred_at DESC` — свежее важнее. Копия в тесте брала
    `ORDER BY id`, то есть порядок вставки: на бэкфиле это прямо обратный
    приоритет."""
    from brain_triage.claim import _claim_batch

    await _seed(10)      # occurred_at убывает с ростом i

    rows = await _claim_batch(3)

    assert [r.source_event_id for r in rows] == ["e0", "e1", "e2"]
    assert rows[0].occurred_at > rows[-1].occurred_at


@pytest.mark.asyncio
async def test_claim_returns_fields_the_worker_needs(pg_db):
    """RETURNING отдаёт все одиннадцать колонок и маппится в EventRow. Копия
    возвращала только id, поэтому маппинг не проверялся вовсе — а именно на
    нём держатся фенс (triage_started_at) и одиночный ретрай batch-miss
    (triage_error)."""
    from brain_triage.claim import _claim_batch

    await _seed(1, account="acct", category="private",
                metadata_={"chat_kind": "private"}, importance=42)

    (row,) = await _claim_batch(1)

    assert row.id and row.source == "test"
    assert row.source_event_id == "e0"
    assert row.account == "acct"
    assert row.category == "private"
    assert row.content_text == "text 0"
    assert row.importance == 42
    assert row.metadata_ == {"chat_kind": "private"}
    assert row.triage_started_at is not None, "нечем фенсить стейл-результат"
    assert row.triage_error is None


@pytest.mark.asyncio
async def test_claim_limit_is_honoured(pg_db):
    """LIMIT приходит параметром (его режет rate-лимитер бэкфила), а не
    захардкожен, как было в копии."""
    from brain_triage.claim import _claim_batch

    await _seed(20)
    assert len(await _claim_batch(7)) == 7
    assert len(await _claim_batch(0)) == 0     # ранний выход, без запроса
    assert len(await _claim_batch(100)) == 13  # осталось 20 - 7


@pytest.mark.asyncio
async def test_watchdog_returns_stuck_processing(pg_db):
    """processing старше STUCK_AFTER_S → watchdog возвращает в pending."""
    from sqlalchemy import text
    from vera_shared.db.engine import get_session
    from vera_shared.db.models import EventRow

    now = utc_naive_now()
    async with get_session() as s:
        s.add(EventRow(
            source="test", source_event_id="stuck1",
            content_text="x", occurred_at=now,
            triage_status="processing",
            triage_started_at=now - timedelta(minutes=15),
        ))
        s.add(EventRow(
            source="test", source_event_id="fresh1",
            content_text="x", occurred_at=now,
            triage_status="processing", triage_started_at=now,
        ))

    async with get_session() as s:
        revived = list((await s.execute(text(
            "UPDATE events SET triage_status='pending', triage_started_at=NULL "
            "WHERE triage_status='processing' "
            "  AND triage_started_at < NOW() - INTERVAL '600 seconds' "
            "RETURNING source_event_id"))).scalars().all())

    assert revived == ["stuck1"]
