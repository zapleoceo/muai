"""Запросы, которые НЕ МОГУТ исполниться на SQLite — и потому не исполнялись
ни одним тестом.

Покрытие честно показывало 0% по телу `dashboard.stats._compute_stats` и по
телу `brain_search.search`: на тестовой SQLite они падают на первой же
конструкции (`FILTER (WHERE …)` ещё пройдёт, а `to_tsvector`, `= ANY(:ids)`
и `NOW()` — нет), поэтому их просто никто не звал. Здесь они выполняются на
том же образе, что в проде.
"""
from __future__ import annotations

import os
from datetime import timedelta

import pytest
import pytest_asyncio
from vera_shared.timeutil import utc_naive_now

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://vera:test@localhost:5433/vera_test",
)

# tests/unit/conftest.py сюда не применяется — свои дефолты окружения.
SECRET = "test-internal-secret"

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

    if engine_mod._engine is not None:
        await close_engine()
    engine = await init_engine(TEST_DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield get_session
    await close_engine()


# ─── dashboard.stats._compute_stats ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_compute_stats_runs_and_aggregates(pg_db):
    """`COUNT(*) FILTER (WHERE …)` по группам плюс окно по usage_log."""
    from dashboard import stats
    from vera_shared.db.engine import get_session
    from vera_shared.db.models import EventRow, UsageLogRow

    now = utc_naive_now()
    async with get_session() as s:
        for i in range(4):
            s.add(EventRow(source="telegram", source_event_id=f"t{i}",
                           content_text="x", occurred_at=now,
                           received_at=now, triage_status="done"))
        s.add(EventRow(source="gmail", source_event_id="g1", content_text="x",
                       occurred_at=now, received_at=now, triage_status="pending"))
        s.add(EventRow(source="gmail", source_event_id="g2", content_text="x",
                       occurred_at=now, received_at=now, triage_status="error"))
        s.add(UsageLogRow(provider="broker", model="m", capability="chat:fast",
                          cost_usd=0.25, workflow="triage", created_at=now))

    got = await stats.get_stats(force=True)

    assert got["total"] == 6
    assert got["done"] == 4
    assert got["pending"] == 1
    assert got["error"] == 1
    assert got["backlog_total"] == 2
    assert dict(got["sources_top"]) == {"telegram": 4, "gmail": 2}
    assert got["cost_today"] == pytest.approx(0.25)
    assert got["triage_1h"] == 1


@pytest.mark.asyncio
async def test_usage_log_window_excludes_old_rows(pg_db):
    """Окно `WHERE created_at >= :month` не должно менять цифры — всё, что
    считается FILTER'ами, внутри него. Строка старше окна в месячную сумму
    не входит и раньше не входила."""
    from dashboard import stats
    from vera_shared.db.engine import get_session
    from vera_shared.db.models import UsageLogRow

    now = utc_naive_now()
    async with get_session() as s:
        s.add(UsageLogRow(provider="b", model="m", capability="c",
                          cost_usd=1.0, workflow="triage", created_at=now))
        s.add(UsageLogRow(provider="b", model="m", capability="c",
                          cost_usd=99.0, workflow="triage",
                          created_at=now - timedelta(days=120)))

    got = await stats.get_stats(force=True)
    assert got["cost_month"] == pytest.approx(1.0), "старая строка попала в месяц"


# ─── brain_search.search ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_fts_branch_runs_on_russian_config(pg_db, monkeypatch):
    """`to_tsvector('russian') @@ to_tsquery('russian', …)` — на SQLite это
    `no such function`, поэтому FTS-ветка поиска не исполнялась никогда."""
    from brain_search import app as bs
    from vera_shared.db.engine import get_session
    from vera_shared.db.models import EventRow

    now = utc_naive_now()
    async with get_session() as s:
        s.add(EventRow(source="telegram", source_event_id="s1",
                       content_text="Договорились о встрече по проекту в Джакарте",
                       occurred_at=now, received_at=now, triage_status="done"))
        s.add(EventRow(source="telegram", source_event_id="s2",
                       content_text="Прогноз погоды на выходные",
                       occurred_at=now, received_at=now, triage_status="done"))

    async def _no_embed(_texts):
        raise bs.LLMCallFailed("нет брокера в тесте")

    async def _synth(**_kw):
        return "ответ", {"provider": "test", "cost_usd": 0.0}

    monkeypatch.setattr(bs, "embed", _no_embed)
    monkeypatch.setattr(bs, "chat_async", _synth)
    monkeypatch.setenv("INTERNAL_SECRET", SECRET)

    res = await bs.search(bs.SearchQuery(q="встреча по проекту", use_agent=False),
                          x_internal_secret=SECRET)

    found = {r.event_id for r in res.results}
    ids = {}
    async with get_session() as s:
        from sqlalchemy import select
        for r in (await s.execute(select(EventRow))).scalars().all():
            ids[r.source_event_id] = r.id
    assert ids["s1"] in found, "русский стеммер не нашёл прямое совпадение"


@pytest.mark.asyncio
async def test_search_time_window_branch(pg_db, monkeypatch):
    """Темпоральная ветка: события вне окна не попадают в выдачу."""
    from brain_search import app as bs
    from vera_shared.db.engine import get_session
    from vera_shared.db.models import EventRow

    now = utc_naive_now()
    async with get_session() as s:
        s.add(EventRow(source="telegram", source_event_id="today",
                       content_text="сегодняшняя запись про отчёт",
                       occurred_at=now, received_at=now, triage_status="done"))
        s.add(EventRow(source="telegram", source_event_id="ancient",
                       content_text="прошлогодняя запись про отчёт",
                       occurred_at=now - timedelta(days=400),
                       received_at=now, triage_status="done"))

    async def _no_embed(_texts):
        raise bs.LLMCallFailed("нет брокера в тесте")

    async def _synth(**_kw):
        return "ответ", {"provider": "test", "cost_usd": 0.0}

    monkeypatch.setattr(bs, "embed", _no_embed)
    monkeypatch.setattr(bs, "chat_async", _synth)
    monkeypatch.setenv("INTERNAL_SECRET", SECRET)

    res = await bs.search(bs.SearchQuery(q="что было сегодня", use_agent=False),
                          x_internal_secret=SECRET)

    previews = " ".join(r.content_preview for r in res.results)
    assert "прошлогодняя" not in previews


# ─── graph_repo: = ANY / expanding IN и degree ──────────────────────────────


@pytest.mark.asyncio
async def test_graph_snapshot_on_postgres(pg_db):
    """Тот же снапшот, что гоняется на SQLite в юнит-тестах, но на настоящем
    диалекте: expanding bindparam в IN, jsonb `attributes->>`, GROUP BY."""
    from vera_shared.graph import repo

    a = await repo.upsert_entity(type="person", name="A", source="s", identifier="a",
                                 attributes={"username": "aaa", "tg_id": 1})
    b = await repo.upsert_entity(type="org", name="B", source="s", identifier="b")
    c = await repo.upsert_entity(type="person", name="C", source="s", identifier="c")
    await repo.upsert_relationship(subject_entity_id=a, object_entity_id=b,
                                   predicate="works_at", confidence=0.8)
    await repo.upsert_relationship(subject_entity_id=a, object_entity_id=c,
                                   predicate="friend_of", confidence=0.7)
    await repo.upsert_membership(parent_entity_id=b, child_entity_id=c, source="s")

    snap = await repo.graph_snapshot(min_degree=1, limit=100)
    by_id = {n["id"]: n for n in snap["nodes"]}

    assert set(by_id) == {a, b, c}
    assert by_id[a]["degree"] == 2
    assert by_id[b]["degree"] == 2      # ребро + членство (родитель)
    assert by_id[c]["degree"] == 2      # ребро + членство (ребёнок)
    assert by_id[a]["username"] == "aaa"    # attributes->>'username'
    assert by_id[a]["tg_id"] == "1"
    assert len(snap["edges"]) == 3      # 2 связи + membership как member_of
