"""MCP на живом Postgres: транзакция READ ONLY и исключение скрытого из поиска.

Юнит-тесты на SQLite проверяют разбор запроса, но не саму транзакцию: SET
TRANSACTION READ ONLY и statement_timeout существуют только в Postgres.
"""
from __future__ import annotations

import os
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text
from vera_shared.timeutil import utc_naive_now

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://vera:test@localhost:5433/vera_test")

pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_INTEGRATION_TESTS"),
    reason="Set RUN_INTEGRATION_TESTS=1 + provide TEST_DATABASE_URL",
)


@pytest_asyncio.fixture
async def pg_db(monkeypatch, pg_schema_reset):
    monkeypatch.setenv("DATABASE_URL", TEST_DB_URL)
    import vera_shared.db.engine as engine_mod
    from vera_shared.db import (  # noqa: F401
        models,
        models_graph,
        models_mcp,
        models_sources,
    )
    from vera_shared.db.engine import close_engine, get_session, init_engine

    if engine_mod._engine is not None:
        await close_engine()
    engine = await init_engine(TEST_DB_URL)
    async with engine.begin() as conn:
        await pg_schema_reset(conn)
    yield get_session
    await close_engine()


async def _seed(status: str = "done") -> int:
    from vera_shared.db.engine import get_session
    from vera_shared.db.models import EventRow

    async with get_session() as s:
        row = EventRow(source="gmail", source_event_id="s1", content_text="quarterly budget",
                       occurred_at=utc_naive_now(), triage_status=status,
                       metadata_={"from": "Bob <bob@example.com>"})
        s.add(row)
        await s.flush()
        return row.id


@pytest.mark.asyncio
async def test_select_runs_and_json_operators_work(pg_db):
    from vera_mcp.sql_guard import run_readonly

    await _seed()
    out = await run_readonly(
        "SELECT metadata->>'from' AS sender, count(*) FILTER (WHERE true) AS n "
        "FROM events GROUP BY 1")
    assert out["columns"] == ["sender", "n"]
    assert out["rows"] == [["Bob <bob@example.com>", 1]]


@pytest.mark.asyncio
async def test_transaction_is_read_only_even_if_the_parser_is_bypassed(pg_db, monkeypatch):
    """Главный барьер: разбор отключён, но Postgres всё равно отказывает."""
    from vera_mcp import sql_guard
    from vera_shared.db.engine import get_session

    await _seed()
    monkeypatch.setattr(sql_guard, "validate_sql", lambda sql: sql)
    cte_write = ("WITH d AS (UPDATE events SET content_text = 'pwned' RETURNING id) "
                 "SELECT id FROM d")
    with pytest.raises(Exception, match="read-only"):
        await sql_guard.run_readonly(cte_write)
    async with get_session() as s:
        got = (await s.execute(text("SELECT content_text FROM events"))).scalar_one()
    assert got == "quarterly budget"


@pytest.mark.asyncio
async def test_statement_timeout_cuts_a_slow_query(pg_db, monkeypatch):
    from vera_mcp import sql_guard

    monkeypatch.setattr(sql_guard, "STATEMENT_TIMEOUT_MS", 200)
    with pytest.raises(Exception, match="statement timeout"):
        await sql_guard.run_readonly("SELECT pg_sleep(5)")


@pytest.mark.asyncio
async def test_row_cap_applies_in_the_database(pg_db):
    from vera_mcp.sql_guard import run_readonly

    out = await run_readonly("SELECT g FROM generate_series(1, 2000) AS g", max_rows=10_000)
    assert out["row_count"] == 500 and out["truncated"] is True


@pytest.mark.asyncio
async def test_hidden_event_is_excluded_from_search_candidates(pg_db):
    """Скрытое событие не попадает ни в FTS-ветку, ни в «последние», ни в ветку времени."""
    from brain_search import retrieval
    from vera_mcp import write_tools
    from vera_shared.db.engine import get_session
    from vera_shared.db.models import EventRow

    event_id = await _seed()
    now = utc_naive_now()
    async with get_session() as s:
        s.add(EventRow(source="gmail", source_event_id="s2", content_text="quarterly report",
                       occurred_at=now, triage_status="done"))

    async def ids(**kw):
        found = await retrieval.fetch_candidates(
            ts_query="quarterly:*", acc_words=[], time_range=None, project=None,
            q_vec=None, limit=50, **kw)
        return {r[0] for r in found.rows}

    class _Ctx:
        request_context = type("R", (), {"request": type("Q", (), {
            "scope": {"mcp_client": "test"}})()})()

    assert event_id in await ids()
    await write_tools.hide_event(event_id, _Ctx())
    assert event_id not in await ids()
    window = (now - timedelta(days=1), now + timedelta(days=1))
    found = await retrieval.fetch_candidates(
        ts_query="", acc_words=[], time_range=window, project=None, q_vec=None, limit=50)
    assert event_id not in {r[0] for r in found.rows}
    await write_tools.unhide_event(event_id, _Ctx())
    assert event_id in await ids()


@pytest.mark.asyncio
async def test_audit_and_undo_round_trip_on_postgres(pg_db):
    from vera_mcp import write_tools

    class _Ctx:
        request_context = type("R", (), {"request": type("Q", (), {
            "scope": {"mcp_client": "test"}})()})()

    event_id = await _seed()
    edited = await write_tools.update_event(event_id, _Ctx(), content_text="changed",
                                            metadata={"k": "v"})
    await write_tools.undo(edited["audit_id"], _Ctx())
    from vera_mcp.read_tools import get_event

    got = await get_event(event_id)
    assert got["content_text"] == "quarterly budget"
    assert got["metadata"] == {"from": "Bob <bob@example.com>"}


@pytest.mark.asyncio
async def test_remember_fact_inserts_dedups_exactly_and_semantically(pg_db, monkeypatch):
    """Общая запись факта на настоящем ON CONFLICT: новое, точный и смысловой дубль."""
    from vera_shared.db.engine import get_session
    from vera_shared.memory import remember as rem

    async def fake_embed(_text):
        return [[1.0, 0.0, 0.0]]

    monkeypatch.setattr(rem, "embed", fake_embed)
    first = await rem.remember_fact("Дима выбрал вариант A", "decision", "ctx", ["t"])
    assert first.deduped is False and first.event_id

    exact = await rem.remember_fact("  Дима выбрал вариант A  ")
    assert (exact.deduped, exact.dedup_reason, exact.event_id) == (True, "exact", first.event_id)

    # вектор первого факта уже записан самой remember_fact (закрывает «слепое окно»)
    near = await rem.remember_fact("Дима остановился на варианте A")
    assert near.dedup_reason == "semantic" and near.similar_event_id == first.event_id
    async with get_session() as s:
        status = (await s.execute(text(
            "SELECT triage_status FROM events WHERE id = :i"), {"i": near.event_id})).scalar_one()
    assert status == "superseded"
