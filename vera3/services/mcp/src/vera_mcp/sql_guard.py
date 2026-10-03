"""`sql_query`: произвольный SELECT/WITH только на чтение.

Два независимых барьера:

1. Разбор текста (`validate_sql`): комментарии и литералы вырезаются
   сканером, остаток — ровно один оператор, начинающийся с SELECT/WITH, без
   слов-мутаций (DML, DDL, COPY, INTO, SET, FOR UPDATE/SHARE) и без опасных
   функций (`set_config`, чтение файлов, advisory-локи и т.п.).
2. Транзакция `READ ONLY` с `statement_timeout` (`run_readonly`): даже если
   разбор что-то пропустит, Postgres откажет в записи. Это главный барьер,
   разбор — защита вглубь и понятное сообщение об ошибке.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection
from vera_shared.db.engine import init_engine

STATEMENT_TIMEOUT_MS = 10_000
MAX_ROWS = 500
MAX_CELL_CHARS = 2_000

_FORBIDDEN_WORDS = (
    "insert", "update", "delete", "merge", "drop", "alter", "create", "truncate",
    "grant", "revoke", "copy", "into", "set", "reset",
)
_FORBIDDEN_FUNCS = (
    "set_config", "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file",
    "lo_import", "lo_export", "lo_get", "dblink", "pg_terminate_backend",
    "pg_cancel_backend", "pg_reload_conf", "pg_advisory_lock", "pg_advisory_xact_lock",
    "pg_try_advisory_lock", "pg_notify", "nextval", "setval",
)
_WORD_RE = re.compile(r"\b(" + "|".join(_FORBIDDEN_WORDS + _FORBIDDEN_FUNCS) + r")\b", re.I)
_LOCKING_RE = re.compile(r"\bfor\s+(no\s+key\s+update|key\s+share|share)\b", re.I)
_DOLLAR_TAG_RE = re.compile(r"\$[A-Za-z_]*\$")


class SqlRejected(ValueError):
    """Запрос не прошёл проверку; сообщение объясняет почему."""


def _skip_quoted(sql: str, i: int, quote: str) -> int:
    """Индекс после закрывающей кавычки (`''` и `""` — экранирование)."""
    n = len(sql)
    i += 1
    while i < n:
        if sql[i] == quote:
            if i + 1 < n and sql[i + 1] == quote:
                i += 2
                continue
            return i + 1
        i += 1
    raise SqlRejected("unterminated quoted string")


def _skip_block_comment(sql: str, i: int) -> int:
    depth = 0
    n = len(sql)
    while i < n:
        if sql.startswith("/*", i):
            depth += 1
            i += 2
        elif sql.startswith("*/", i):
            depth -= 1
            i += 2
            if depth == 0:
                return i
        else:
            i += 1
    raise SqlRejected("unterminated /* comment")


def strip_literals(sql: str) -> str:
    """SQL без комментариев; строки и идентификаторы в кавычках заменены на ''."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if sql.startswith("--", i):
            nl = sql.find("\n", i)
            i = n if nl < 0 else nl
        elif sql.startswith("/*", i):
            i = _skip_block_comment(sql, i)
            out.append(" ")
        elif ch in "'\"":
            i = _skip_quoted(sql, i, ch)
            out.append("''")
        elif ch == "$" and (m := _DOLLAR_TAG_RE.match(sql, i)):
            end = sql.find(m.group(0), m.end())
            if end < 0:
                raise SqlRejected("unterminated dollar-quoted string")
            i = end + len(m.group(0))
            out.append("''")
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def validate_sql(sql: str) -> str:
    """Вернуть оператор без хвостового `;` или бросить SqlRejected."""
    code = strip_literals(sql).strip()
    while code.endswith(";"):
        code = code[:-1].rstrip()
    if not code:
        raise SqlRejected("empty query")
    if ";" in code:
        raise SqlRejected("only a single statement is allowed")
    first = re.match(r"[A-Za-z]+", code)
    if first is None or first.group(0).lower() not in {"select", "with"}:
        raise SqlRejected("only SELECT / WITH queries are allowed")
    if m := _WORD_RE.search(code):
        raise SqlRejected(f"'{m.group(0).lower()}' is not allowed in a read-only query")
    if _LOCKING_RE.search(code):
        raise SqlRejected("row locking (FOR UPDATE/SHARE) is not allowed")
    return sql.strip().rstrip(";").rstrip()


def _cell(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict | list):
        return _cell(str(value)) if len(str(value)) > MAX_CELL_CHARS else value
    if isinstance(value, bytes | memoryview):
        return f"<{len(bytes(value))} bytes>"
    text = str(value)
    return text if len(text) <= MAX_CELL_CHARS else text[:MAX_CELL_CHARS] + "…"


async def _enter_read_only(conn: AsyncConnection) -> None:
    # Тесты идут на SQLite, где SET TRANSACTION нет; ограничение там — только
    # разбор. На Postgres (прод и интеграционные тесты) транзакция READ ONLY.
    if conn.dialect.name != "postgresql":
        return
    await conn.exec_driver_sql("SET TRANSACTION READ ONLY")
    await conn.exec_driver_sql(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")


async def run_readonly(sql: str, max_rows: int = MAX_ROWS) -> dict[str, Any]:
    """Выполнить проверенный запрос; не больше `max_rows` строк (потолок MAX_ROWS)."""
    statement = validate_sql(sql)
    cap = max(1, min(max_rows, MAX_ROWS))
    # LIMIT снаружи подзапроса: лишняя строка нужна, чтобы честно сказать «обрезано»
    wrapped = f"SELECT * FROM ({statement}) AS _q LIMIT {cap + 1}"
    engine = await init_engine()
    async with engine.connect() as conn:
        await _enter_read_only(conn)
        result = await conn.exec_driver_sql(wrapped)
        columns = list(result.keys())
        rows = result.fetchall()
        await conn.rollback()
    return {
        "columns": columns,
        "rows": [[_cell(v) for v in row] for row in rows[:cap]],
        "row_count": min(len(rows), cap),
        "truncated": len(rows) > cap,
    }
