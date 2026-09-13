"""Полнотекстовые выражения brain-search (fts.py).

tsvector на SQLite не исполнить, поэтому проверяется строка SQL: условие
обязано совпадать с выражениями индексов миграции 031 дословно, иначе
планировщик уйдёт в seq scan по 445 тыс. строк (замер: 7.7 с).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from brain_search import app as bs
from brain_search.fts import FTS_CONFIGS, build_ts_query, fts_match_sql, fts_rank_sql

_MIGRATION = (Path(__file__).resolve().parents[2] / "infra" / "migrations"
              / "031_events_fts_multilingual.sql")


def test_query_keeps_prefix_or_shape():
    assert build_ts_query(["оплата", "Itstep"]) == "оплата:* | Itstep:*"
    assert build_ts_query([]) == ""


def test_foreign_stopwords_dropped_russian_words_kept():
    assert build_ts_query(["the", "invoice", "yang", "dan", "и"]) == "invoice:* | и:*"


def test_app_query_uses_shared_builder():
    ts, _ = bs._ts_query("payments for the villa")
    assert ts == "payments:* | villa:*"


def test_match_covers_every_config_with_or():
    sql = fts_match_sql()
    for cfg in ("russian", "indonesian"):
        assert f"to_tsvector('{cfg}', content_text) @@ to_tsquery('{cfg}', :tsq)" in sql
    assert sql.count(" OR ") == len(FTS_CONFIGS) - 1


def test_rank_is_russian_first_so_old_order_survives():
    """Строка, найденная русским, получает ровно прежний ts_rank(russian):
    COALESCE берёт его первым и уходит к indonesian только при нуле."""
    sql = fts_rank_sql()
    assert sql.startswith(
        "COALESCE(NULLIF(ts_rank(to_tsvector('russian', content_text),"
        " to_tsquery('russian', :tsq)), 0), ")
    assert sql.index("'russian'") < sql.index("'indonesian'")


def test_param_name_is_respected():
    assert ":q2" in fts_match_sql("q2")
    assert ":tsq" not in fts_rank_sql("q2")


def test_every_config_has_a_matching_index_expression():
    body = _MIGRATION.read_text(encoding="utf-8")
    indexed = set(re.findall(r"gin \(to_tsvector\('(\w+)', content_text\)\)", body))
    assert indexed == set(FTS_CONFIGS)
    assert not re.search(r"^\s*BEGIN", body, re.M), "CONCURRENTLY в транзакции запрещён"
    assert body.count("CREATE INDEX CONCURRENTLY IF NOT EXISTS") == len(FTS_CONFIGS)


class _Result:
    def all(self):
        return []


class _CapturingSession:
    def __init__(self):
        self.sql: list[str] = []

    async def execute(self, stmt, _params=None):
        self.sql.append(str(stmt))
        return _Result()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_retrieval_fts_branch_uses_multilingual_sql():
    from brain_search import retrieval
    s = _CapturingSession()
    found = await retrieval._primary(s, ts_query="pembayaran:*", acc_words=[],
                                     time_range=None, project=None, q_vec=None,
                                     with_vec=False, limit=10)
    assert found.mode == "fts"
    assert fts_match_sql() in s.sql[0]
    assert f"{fts_rank_sql()} AS rank" in s.sql[0]


@pytest.mark.asyncio
async def test_agent_search_events_uses_multilingual_sql(monkeypatch):
    import vera_shared.db.engine as engine
    from brain_search import agent
    s = _CapturingSession()
    monkeypatch.setattr(engine, "get_session", lambda: s)
    res = await agent._exec_search_events("pembayaran siswa")
    assert res["found"] == 0
    assert fts_match_sql() in s.sql[0]
    assert f"ORDER BY {fts_rank_sql()} DESC" in s.sql[0]
