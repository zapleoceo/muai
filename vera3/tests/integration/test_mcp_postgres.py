"""MCP на живом Postgres: роль vera_ro, транзакция READ ONLY, скрытое в поиске.

Юнит-тесты на SQLite проверяют разбор запроса, но не права и не транзакцию:
роли, GRANT и SET TRANSACTION существуют только в Postgres. Миграция 037
накатывается настоящая, как на проде.
"""
from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from vera_shared.timeutil import utc_naive_now

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://vera:test@localhost:5433/vera_test")
RO_PASSWORD = "ro-test-password"
MIGRATION = Path(__file__).resolve().parents[2] / "infra" / "migrations" / "037_mcp_ro_role.sql"

pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_INTEGRATION_TESTS"),
    reason="Set RUN_INTEGRATION_TESTS=1 + provide TEST_DATABASE_URL",
)


async def _apply_ro_role(engine) -> str:
    """Накатывает настоящую миграцию 037 и возвращает URL роли vera_ro."""
    import asyncpg

    async with engine.begin() as conn:
        await conn.execute(text(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now(), "
            "note text)"))
    # asyncpg.execute по simple-протоколу принимает файл целиком (BEGIN … COMMIT)
    dsn = make_url(TEST_DB_URL).set(drivername="postgresql").render_as_string(
        hide_password=False)
    raw = await asyncpg.connect(dsn)
    try:
        await raw.execute(MIGRATION.read_text(encoding="utf-8"))
        await raw.execute(f"ALTER ROLE vera_ro PASSWORD '{RO_PASSWORD}'")
    finally:
        await raw.close()
    url = make_url(TEST_DB_URL).set(username="vera_ro", password=RO_PASSWORD)
    return url.render_as_string(hide_password=False)


@pytest_asyncio.fixture
async def pg_db(monkeypatch, pg_schema_reset):
    monkeypatch.setenv("DATABASE_URL", TEST_DB_URL)
    import vera_shared.db.engine as engine_mod
    from vera_mcp.ro_engine import forget_ro_engine
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
    monkeypatch.setenv("MCP_RO_DATABASE_URL", await _apply_ro_role(engine))
    yield get_session
    await forget_ro_engine()
    await close_engine()


class _Ctx:
    request_context = type("R", (), {"request": type("Q", (), {
        "scope": {"mcp_client": "test"}})()})()


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


# ─── sql_query под ролью vera_ro ─────────────────────────────────────────────


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
async def test_query_runs_in_a_read_only_transaction_as_a_non_superuser(pg_db):
    from vera_mcp.sql_guard import run_readonly

    out = await run_readonly(
        "SELECT current_setting('transaction_read_only') AS ro, current_user AS who, "
        "(SELECT rolsuper FROM pg_roles WHERE rolname = current_user) AS su")
    assert out["rows"] == [["on", "vera_ro", False]]


BYPASSES = [
    # опасный SQL спрятан в литерале — разбор текста его не видит
    "SELECT query_to_xml('update events set content_text = ''pwned''', true, false, '')",
    "SELECT query_to_xml('select pg_read_file(''/etc/passwd'')', true, false, '')",
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT rolpassword FROM pg_authid",
    "SELECT * FROM gmail_accounts",
    "SELECT * FROM telegram_sessions",
    "SELECT * FROM app_control",
    "SELECT * FROM slack_auth",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("sql", BYPASSES)
async def test_database_refuses_even_when_the_parser_is_bypassed(pg_db, monkeypatch, sql):
    """Главный барьер — права роли, а не разбор: отключаем разбор и убеждаемся."""
    from vera_mcp import sql_guard

    await _seed()
    monkeypatch.setattr(sql_guard, "validate_sql", lambda s: s)
    # Postgres отказывает разными словами: UPDATE внутри query_to_xml режется
    # ещё до проверки прав («not allowed in a non-volatile function»). Важен
    # отказ и неизменённые данные ниже, а не формулировка.
    with pytest.raises(Exception, match="permission denied|read-only|must be|not allowed"):
        await sql_guard.run_readonly(sql)
    async with pg_db() as s:
        content = (await s.execute(text("SELECT content_text FROM events"))).scalar_one()
    assert content == "quarterly budget"


@pytest.mark.asyncio
async def test_other_sessions_queries_are_hidden(pg_db):
    from vera_mcp.sql_guard import run_readonly

    out = await run_readonly(
        "SELECT query FROM pg_stat_activity WHERE usename = 'vera' AND query <> ''")
    assert all(row[0] == "<insufficient privilege>" for row in out["rows"])


@pytest.mark.asyncio
async def test_secret_tables_are_not_granted_but_content_tables_are(pg_db):
    from vera_mcp.sql_guard import run_readonly

    out = await run_readonly(
        "SELECT table_name FROM information_schema.role_table_grants "
        "WHERE grantee = 'vera_ro' AND privilege_type = 'SELECT' ORDER BY 1", max_rows=100)
    granted = {row[0] for row in out["rows"]}
    assert {"events", "entities", "relationships", "mcp_audit"} <= granted
    assert not granted & {"gmail_accounts", "telegram_sessions", "instagram_sessions",
                          "slack_auth", "app_control", "trello_boards", "api_tokens"}


@pytest.mark.asyncio
async def test_refuses_without_url_or_with_a_superuser_url(pg_db, monkeypatch):
    from vera_mcp.ro_engine import ReadOnlyUnavailable, forget_ro_engine
    from vera_mcp.sql_guard import run_readonly

    await forget_ro_engine()
    monkeypatch.delenv("MCP_RO_DATABASE_URL")
    with pytest.raises(ReadOnlyUnavailable, match="not set"):
        await run_readonly("SELECT 1")
    monkeypatch.setenv("MCP_RO_DATABASE_URL", TEST_DB_URL)     # роль vera — суперпользователь
    with pytest.raises(ReadOnlyUnavailable, match="superuser"):
        await run_readonly("SELECT 1")


@pytest.mark.asyncio
async def test_migration_is_idempotent(pg_db):
    from vera_mcp.ro_engine import forget_ro_engine
    from vera_shared.db.engine import init_engine

    await _apply_ro_role(await init_engine())
    await forget_ro_engine()
    from vera_mcp.sql_guard import run_readonly

    assert (await run_readonly("SELECT 1"))["rows"] == [[1]]


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


# ─── скрытое, аудит, remember ────────────────────────────────────────────────


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

    async def ids():
        found = await retrieval.fetch_candidates(
            ts_query="quarterly:*", acc_words=[], time_range=None, project=None,
            q_vec=None, limit=50)
        return {r[0] for r in found.rows}

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
    from vera_mcp.read_tools import get_event

    event_id = await _seed()
    edited = await write_tools.update_event(event_id, _Ctx(), content_text="changed",
                                            metadata={"k": "v"})
    await write_tools.undo(edited["audit_id"], _Ctx())
    got = await get_event(event_id)
    assert got["content_text"] == "quarterly budget"
    assert got["metadata"] == {"from": "Bob <bob@example.com>"}


@pytest.mark.asyncio
async def test_remember_is_atomic_with_its_audit_row(pg_db, monkeypatch):
    from vera_mcp import write_tools
    from vera_shared.memory import remember as rem

    async def no_embed(_t):
        return []

    monkeypatch.setattr(rem, "embed", no_embed)
    res = await write_tools.remember("a durable fact", _Ctx())
    assert res["audit_id"] and res["deduped"] is False
    dup = await write_tools.remember("a durable fact", _Ctx())
    assert dup["audit_id"] is None and dup["dedup_reason"] == "exact"

    async def boom(*_a, **_k):
        raise RuntimeError("audit failed")

    monkeypatch.setattr(write_tools.audit, "record", boom)
    with pytest.raises(RuntimeError):
        await write_tools.remember("another fact", _Ctx())
    async with pg_db() as s:
        n = (await s.execute(text(
            "SELECT count(*) FROM events WHERE content_text = 'another fact'"))).scalar_one()
    assert n == 0, "событие осталось без записи журнала"


@pytest.mark.asyncio
async def test_row_lock_serialises_concurrent_edits(pg_db):
    """FOR UPDATE реально берётся: вторая правка ждёт первую, а не затирает её."""
    import asyncio

    from vera_shared.db.engine import get_session
    from vera_shared.events import edit

    event_id = await _seed()
    order: list[str] = []

    async def first():
        async with get_session() as s:
            await edit.update_event(s, event_id, metadata_patch={"a": 1})
            order.append("first-locked")
            await asyncio.sleep(0.5)
            order.append("first-commit")

    async def second():
        await asyncio.sleep(0.1)
        async with get_session() as s:
            await edit.update_event(s, event_id, metadata_patch={"b": 2})
            order.append("second-done")

    await asyncio.gather(first(), second())
    assert order == ["first-locked", "first-commit", "second-done"]
    async with get_session() as s:
        row = await edit.load_row(s, event_id)
        assert row.metadata_ == {"from": "Bob <bob@example.com>", "a": 1, "b": 2}


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
