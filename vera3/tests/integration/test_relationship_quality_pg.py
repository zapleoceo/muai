"""Качество связей на живом Postgres: ON CONFLICT при гонке и чистка с откатом.

SQLite не повторяет два главных места: гонку двух писателей на уникальном
индексе `uq_relationships_spo` и `SELECT … FOR UPDATE` при применении плана.
"""
from __future__ import annotations

import asyncio
import os

import pytest
import pytest_asyncio
from sqlalchemy import text as sa_text

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://vera:test@localhost:5433/vera_test",
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_INTEGRATION_TESTS"),
    reason="Set RUN_INTEGRATION_TESTS=1 + provide TEST_DATABASE_URL",
)


@pytest_asyncio.fixture
async def pg_db(monkeypatch, pg_schema_reset):
    monkeypatch.setenv("TOKEN_SECRET", "test-secret-for-integration")
    monkeypatch.setenv("DATABASE_URL", TEST_DB_URL)
    import vera_shared.db.engine as engine_mod
    from vera_shared.db import models, models_graph, models_sources  # noqa: F401
    from vera_shared.db.engine import close_engine, get_session, init_engine

    if engine_mod._engine is not None:
        await close_engine()
    engine = await init_engine(TEST_DB_URL)
    async with engine.begin() as conn:
        await pg_schema_reset(conn)
        await conn.execute(sa_text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_relationships_spo "
            "ON relationships (subject_entity_id, predicate, object_entity_id)"))
    yield get_session
    await close_engine()


async def _pair():
    from vera_shared.graph import repo
    a = await repo.upsert_entity(type="person", name="Ivan Petrov", source="s", identifier="a")
    b = await repo.upsert_entity(type="person", name="Anna Lee", source="s", identifier="b")
    return a, b


async def _rows(get_session):
    async with get_session() as s:
        return [tuple(r) for r in (await s.execute(sa_text(
            "SELECT subject_entity_id, predicate, object_entity_id, is_current "
            "FROM relationships ORDER BY id"))).all()]


@pytest.mark.asyncio
async def test_concurrent_upserts_of_one_triple_do_not_raise(pg_db):
    from vera_shared.graph import repo
    a, b = await _pair()
    results = await asyncio.gather(*(
        repo.upsert_relationship(subject_entity_id=a, object_entity_id=b,
                                 predicate="works_at", confidence=0.7,
                                 derived_from_event_id=i) for i in range(8)))
    assert results.count(True) == 1
    assert len(await _rows(pg_db)) == 1


@pytest.mark.asyncio
async def test_concurrent_symmetric_writers_leave_one_row(pg_db):
    from vera_shared.graph import repo
    a, b = await _pair()
    await asyncio.gather(
        *(repo.upsert_relationship(subject_entity_id=x, object_entity_id=y,
                                   predicate="friend_of", derived_from_event_id=1)
          for x, y in [(a, b), (b, a)] * 4))
    assert len(await _rows(pg_db)) == 1


@pytest.mark.asyncio
async def test_cleanup_plan_apply_and_undo(pg_db, tmp_path):
    from vera_shared.graph.rel_cleanup import build_plan, plan_document
    from vera_shared.graph.rel_cleanup_apply import apply_plan, undo_report
    from vera_shared.graph.rel_cleanup_snapshot import load_snapshot
    a, b = await _pair()
    async with pg_db() as s:
        await s.execute(sa_text(
            "INSERT INTO relationships (subject_entity_id, predicate, object_entity_id, "
            "confidence, fact, first_seen_at, last_seen_at, is_current) VALUES "
            "(:a, 'reports_to', :b, 0.9, 'Ivan Petrov Anna Lee', now(), now(), true)"),
            {"a": a, "b": b})
    before = await _rows(pg_db)
    snapshot = await load_snapshot(1)
    plan = plan_document(build_plan(snapshot), "pg")
    assert plan["counts"] == {"convert_inverse": 1}

    report = tmp_path / "rollback.json"
    await apply_plan(plan, report)
    assert await _rows(pg_db) == [(b, "boss_of", a, True)]
    assert await undo_report(report) == 1
    assert await _rows(pg_db) == before
