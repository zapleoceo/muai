"""sql_query: разбор текста запроса и выполнение (SQLite; транзакция READ ONLY — в integration)."""
from __future__ import annotations

from datetime import datetime

import pytest
from vera_mcp import sql_guard
from vera_mcp.sql_guard import SqlRejected, run_readonly, strip_literals, validate_sql

REJECTED = [
    "INSERT INTO events (source) VALUES ('x')",
    "UPDATE events SET content_text = ''",
    "DELETE FROM events",
    "DROP TABLE events",
    "ALTER TABLE events ADD COLUMN x int",
    "CREATE TABLE t (a int)",
    "TRUNCATE events",
    "GRANT ALL ON events TO public",
    "COPY events TO '/tmp/x'",
    "SELECT 1; SELECT 2",
    "SELECT 1; DROP TABLE events",
    "SELECT * INTO newtable FROM events",
    "SELECT set_config('transaction_read_only', 'off', false)",
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT nextval('events_id_seq')",
    "SELECT * FROM events FOR UPDATE",
    "SELECT * FROM events FOR SHARE",
    "WITH d AS (DELETE FROM events RETURNING id) SELECT * FROM d",
    "WITH d AS (UPDATE events SET importance = 1 RETURNING id) SELECT id FROM d",
    "SET TRANSACTION READ WRITE",
    "SELECT query_to_xml('select 1', true, false, '')",
    "SELECT cursor_to_xml(c, 1, false, false, '') FROM x",
    "VACUUM",
    "EXPLAIN SELECT 1",
    "",
    "   ;  ",
    "SELECT 1 -- ok\n; DELETE FROM events",
    "/* nested /* comment */ still comment */ DELETE FROM events",
    "SELECT 'unterminated",
    "SELECT $$ never closed",
    "SELECT 1 /* never closed",
]


@pytest.mark.parametrize("sql", REJECTED)
def test_rejects(sql):
    with pytest.raises(SqlRejected):
        validate_sql(sql)


ACCEPTED = [
    "SELECT 1",
    "select count(*) from events;",
    "WITH x AS (SELECT 1 AS a) SELECT a FROM x",
    # слова-мутации внутри литералов и идентификаторов в кавычках — не мутации
    "SELECT 'delete; drop table x' AS note",
    "SELECT $q$update; insert$q$ AS note",
    'SELECT 1 AS "set"',
    "SELECT 1 AS set",
    "SELECT 1 AS reset",
    "SELECT 'it''s; fine'",
    "SELECT 1 -- DELETE FROM events",
    "SELECT /* DROP TABLE x */ 1",
    "SELECT current_setting('server_version') AS offset_of_set",
]


@pytest.mark.parametrize("sql", ACCEPTED)
def test_accepts(sql):
    validate_sql(sql)


def test_validate_returns_statement_without_trailing_semicolon():
    assert validate_sql("  SELECT 1 ;; ") == "SELECT 1"


def test_strip_literals_blanks_strings_and_comments():
    assert strip_literals("SELECT 'a;b' /* x */ -- y\n, \"c;d\"") == "SELECT ''   \n, ''"


@pytest.mark.asyncio
async def test_runs_select_and_serializes_cells(sqlite_db, ro_env):
    from vera_shared.db.engine import get_session
    from vera_shared.db.models import EventRow

    async with get_session() as s:
        s.add(EventRow(id=1, source="gmail", source_event_id="a", content_text="x" * 3000,
                       occurred_at=datetime(2026, 1, 2, 3), triage_status="done"))
    out = await run_readonly("SELECT id, source, content_text, occurred_at FROM events;")
    assert out["columns"] == ["id", "source", "content_text", "occurred_at"]
    row = out["rows"][0]
    assert row[:2] == [1, "gmail"]
    assert len(row[2]) == sql_guard.MAX_CELL_CHARS + 1 and row[2].endswith("…")
    assert out["row_count"] == 1 and out["truncated"] is False


@pytest.mark.asyncio
async def test_row_cap_sets_truncated(sqlite_db, ro_env):
    out = await run_readonly(
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 50) "
        "SELECT i FROM n", max_rows=10)
    assert out["row_count"] == 10 and out["truncated"] is True
    assert [r[0] for r in out["rows"]] == list(range(1, 11))


@pytest.mark.asyncio
async def test_cap_never_exceeds_hard_limit(sqlite_db, ro_env):
    out = await run_readonly(
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 800) "
        "SELECT i FROM n", max_rows=10_000)
    assert out["row_count"] == sql_guard.MAX_ROWS and out["truncated"] is True


@pytest.mark.asyncio
async def test_rejected_query_never_reaches_the_database(sqlite_db, ro_env):
    with pytest.raises(SqlRejected):
        await run_readonly("DELETE FROM events")


def test_cell_conversions():
    from datetime import date
    from decimal import Decimal

    assert sql_guard._cell(Decimal("1.5")) == 1.5
    assert sql_guard._cell(date(2026, 1, 2)) == "2026-01-02"
    assert sql_guard._cell(b"abc") == "<3 bytes>"
    assert sql_guard._cell(None) is None
    assert sql_guard._cell({"a": 1}) == {"a": 1}
    assert sql_guard._cell({"a": "x" * 3000}).endswith("…")


@pytest.mark.asyncio
async def test_refuses_when_the_read_only_url_is_not_configured(sqlite_db, monkeypatch):
    from vera_mcp.ro_engine import ReadOnlyUnavailable, forget_ro_engine

    await forget_ro_engine()
    monkeypatch.delenv("MCP_RO_DATABASE_URL", raising=False)
    with pytest.raises(ReadOnlyUnavailable, match="MCP_RO_DATABASE_URL is not set"):
        await run_readonly("SELECT 1")


@pytest.mark.asyncio
async def test_ro_engine_is_cached_per_url_and_rebuilt_on_change(ro_env, monkeypatch, tmp_path):
    from vera_mcp.ro_engine import get_ro_engine

    first = await get_ro_engine()
    assert await get_ro_engine() is first
    monkeypatch.setenv("MCP_RO_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'other.db'}")
    assert await get_ro_engine() is not first


@pytest.mark.asyncio
async def test_superuser_role_is_refused(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock, patch

    from vera_mcp import ro_engine

    conn = MagicMock()
    conn.execute = AsyncMock(return_value=MagicMock(scalar_one=lambda: True))
    engine = MagicMock()
    engine.dialect.name = "postgresql"
    engine.connect.return_value.__aenter__ = AsyncMock(return_value=conn)
    engine.connect.return_value.__aexit__ = AsyncMock(return_value=False)
    engine.dispose = AsyncMock()
    await ro_engine.forget_ro_engine()
    monkeypatch.setenv("MCP_RO_DATABASE_URL", "postgresql+asyncpg://x:y@h/db")
    with patch.object(ro_engine, "create_async_engine", return_value=engine), \
         pytest.raises(ro_engine.ReadOnlyUnavailable, match="superuser"):
        await ro_engine.get_ro_engine()
    engine.dispose.assert_awaited_once()



_SERIES = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 2000) "


@pytest.mark.asyncio
@pytest.mark.parametrize("tail", ["--", "-- trailing comment", "/* ok */ --"])
async def test_trailing_line_comment_keeps_the_row_limit(sqlite_db, ro_env, monkeypatch, tail):
    # Однострочная обёртка `SELECT * FROM (<запрос>) AS _q LIMIT n`: хвостовой `--`
    # комментировал её конец, и законный запрос с комментарием падал синтаксисом.
    from sqlalchemy.ext.asyncio import AsyncConnection

    sent: list[str] = []
    original = AsyncConnection.exec_driver_sql

    async def spy(self, statement, *args, **kwargs):
        sent.append(statement)
        return await original(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncConnection, "exec_driver_sql", spy)
    out = await run_readonly(f"{_SERIES}SELECT i FROM n {tail}", max_rows=5)
    assert out["row_count"] == 5 and out["truncated"] is True
    monkeypatch.setattr(AsyncConnection, "exec_driver_sql", original)
    from vera_mcp.ro_engine import get_ro_engine
    async with (await get_ro_engine()).connect() as conn:
        produced = (await conn.exec_driver_sql(sent[-1])).fetchall()
    assert len(produced) == 6


@pytest.mark.parametrize("query", [
    f"{_SERIES}SELECT i FROM n) AS _q --",
    f"{_SERIES}SELECT i FROM n) AS _q /* x */",
    "SELECT (1",
    "SELECT 1)",
])
def test_unbalanced_parentheses_are_rejected(query):
    # Своя `)` закрывала бы подзапрос обёртки раньше LIMIT: база отдала бы всю выборку.
    with pytest.raises(SqlRejected, match="parenthes"):
        validate_sql(query)


def test_parentheses_inside_literals_and_comments_do_not_count():
    assert validate_sql("SELECT ')' AS a, '(' AS b /* ) */ -- (")
