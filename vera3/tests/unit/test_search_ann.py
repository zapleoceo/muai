"""Смысловой отбор кандидатов в brain-search (ANN) и скоринг по косинусу из БД.

До ANN вектор запроса только переупорядочивал ≤200 строк, уже найденных
полнотекстом: текст без общих с вопросом слов не находился никогда. Здесь —
всё, что проверяемо без Postgres: форма SQL (выражение в запросе обязано
совпасть с выражением индекса), объединение с основной выборкой, выбор
ветки по наличию колонки/индекса и ранжирование по `vec_sim`.
"""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from brain_search import ann, retrieval
from brain_search.scoring import row_similarity, score_rows
from vera_shared.db import vectors


class _Row(tuple):
    """Строка с доступом по позиции и по имени, как sqlalchemy Row."""

    def __new__(cls, values, **named):
        obj = super().__new__(cls, values)
        obj.__dict__.update(named)
        return obj


def _row(eid, emb=None, rank=0.0, account="", source="gmail", **named):
    return _Row((eid, source, f"s{eid}", datetime(2026, 9, 1), "текст", None,
                 emb, rank, account), **named)


# ─── форма SQL ──────────────────────────────────────────────────────────────


def test_query_repeats_the_index_expression_verbatim():
    """Планировщик берёт выражение-индекс, только если запрос повторяет его
    дословно. Разъедутся `::bit(N)` — поиск молча уйдёт в seq scan ~1 ГБ."""
    index_expr = "(binary_quantize(embedding_vec)::bit(1024))"
    assert index_expr in vectors.ann_index_sql(1024)
    assert "bit_hamming_ops" in vectors.ann_index_sql(1024)
    assert "CONCURRENTLY" in vectors.ann_index_sql(1024)

    sql = vectors.ann_candidates_sql(dims=1024, where="x = 1")
    assert "(binary_quantize(ee.embedding_vec)::bit(1024)) <~>" in sql
    assert "binary_quantize(CAST(:q AS halfvec(1024)))::bit(1024)" in sql
    assert "LIMIT :ann_k" in sql and "LIMIT :ann_top" in sql
    # точный пересчёт косинусом по halfvec после грубого шага
    assert "ORDER BY c.embedding_vec <=> CAST(:q AS halfvec(1024))" in sql
    assert "(x = 1)" in sql and "JOIN events" not in sql


def test_filters_on_events_need_the_join():
    sql = vectors.ann_candidates_sql(dims=3, where="source <> 'x'", join_events=True)
    assert "JOIN events ON events.id = ee.event_id" in sql


@pytest.mark.parametrize(("ann_k", "ef"), [(1000, "1000"), (5000, "1000"), (10, "40")])
def test_ef_search_never_below_limit_and_within_pgvector_bounds(ann_k, ef):
    """ef_search < LIMIT — HNSW молча отдаёт ef_search строк вместо LIMIT."""
    assert vectors.ann_settings_params(ann_k)["ef"] == ef


def test_ann_query_has_a_transaction_scoped_timeout():
    """Снесённый индекс без рестарта превращал бы поиск в минутный seq scan по
    всему корпусу — у смыслового запроса свой потолок, и только на транзакцию,
    чтобы не утечь в пул соединений."""
    sql = str(vectors.ANN_SETTINGS_SQL)
    assert "set_config('statement_timeout', :timeout, true)" in sql
    assert vectors.ann_settings_params(1000)["timeout"] == str(vectors.ANN_STATEMENT_TIMEOUT_MS)
    assert 0 < vectors.ANN_STATEMENT_TIMEOUT_MS <= 10_000


def test_similarity_column_goes_last_so_positions_hold():
    """scoring читает rank и account по позициям 7 и 8 — вставка vec_sim
    в середину сдвинула бы их, и ts_rank стал бы косинусом."""
    sql = str(retrieval._select(extra_cols="0.0 AS rank, account", join="JOIN",
                                where="TRUE", order="id", limit_sql="5",
                                with_vec=True))
    select_list = sql.split("FROM events")[0]
    assert select_list.index("AS embedding") < select_list.index("AS rank")
    assert select_list.rstrip().endswith("AS vec_sim")
    plain = str(retrieval._select(extra_cols="0.0 AS rank", join="JOIN",
                                  where="TRUE", order="id", limit_sql="5"))
    assert "vec_sim" not in plain and "ee.embedding," in plain


def test_ann_rows_have_the_primary_shape():
    sql = str(ann.ann_rows_sql("TRUE"))
    cols = sql.split("FROM best JOIN")[0].split("SELECT events.id")[1]
    for name in ("NULL AS embedding", "0.0 AS rank", "events.account", "AS vec_sim"):
        assert name in cols


def test_semantic_filter_keeps_project_and_window_but_not_text():
    project = SimpleNamespace(name="itstep", account_like=["itstep.org"], chats=[])
    rng = (datetime(2026, 9, 1), datetime(2026, 9, 2))

    where, params = retrieval.semantic_filter(project, rng)
    assert "project = :pname" in where and params["t_start"] == rng[0]

    where, params = retrieval.semantic_filter(None, rng)
    assert "occurred_at >= :t_start" in where and "vera_chat" in where
    assert "to_tsquery" not in where

    where, params = retrieval.semantic_filter(None, None)
    assert where.startswith("TRUE") and "conversation_with_me" in where
    assert params == {}


# ─── объединение и выбор ветки ──────────────────────────────────────────────


def test_merge_keeps_primary_and_adds_only_new_ids():
    primary = [_row(1, vec_sim=0.5), _row(2, vec_sim=0.95)]
    semantic = [_row(2, vec_sim=0.9), _row(3, vec_sim=0.8)]
    merged = ann.merge_candidates(primary, semantic)
    assert [r[0] for r in merged] == [1, 2, 3]
    assert merged[1] is primary[1]


def test_merge_lifts_similarity_found_through_a_chunk():
    """Длинное письмо нашлось полнотекстом с косинусом всего письма 0.3, а ANN
    нашёл его же через кусок с 0.85 — событие одно, сходство лучшее, а
    ts_rank/account основной выборки на своих позициях."""
    primary = [_row(1, rank=0.7, account="a@x", vec_sim=0.3)]
    merged = ann.merge_candidates(primary, [_row(1, vec_sim=0.85)])
    assert len(merged) == 1
    assert merged[0].vec_sim == 0.85
    assert merged[0][7] == 0.7 and merged[0][8] == "a@x"
    assert row_similarity(merged[0], [1.0]) == 0.85


def test_ann_sql_unions_chunks_and_keeps_one_row_per_event():
    plain = str(ann.ann_rows_sql("TRUE"))
    assert "event_chunk_embeddings" not in plain
    sql = str(ann.ann_rows_sql("source <> 'x'", with_chunks=True))
    assert "UNION ALL" in sql and "FROM event_chunk_embeddings ee" in sql
    assert "MAX(sim)" in sql and "GROUP BY event_id" in sql
    # кусок ищется тем же выражением, что в его индексе
    assert "(binary_quantize(ee.embedding_vec)::bit(1024)) <~>" in sql.split("UNION ALL")[1]
    assert sql.rstrip().endswith("LIMIT :ann_top")


class _Ctx:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc):
        return False


def _patch_fetch(monkeypatch, *, column: bool, index: bool, semantic):
    primary = retrieval.Candidates([_row(1)], "fts", ["itstep"])
    monkeypatch.setattr(retrieval, "get_session", lambda: _Ctx())
    monkeypatch.setattr(retrieval, "_primary", AsyncMock(return_value=primary))
    monkeypatch.setattr(retrieval, "vector_column_available", AsyncMock(return_value=column))
    monkeypatch.setattr(retrieval, "ann_index_available", AsyncMock(return_value=index))
    fetch = AsyncMock(return_value=semantic)
    monkeypatch.setattr(retrieval, "fetch_ann_rows", fetch)
    return fetch


@pytest.mark.asyncio
async def test_ann_candidates_are_added_on_top_of_fts(monkeypatch):
    """Главный сценарий: строка без общих слов с вопросом доезжает до скоринга."""
    fetch = _patch_fetch(monkeypatch, column=True, index=True,
                         semantic=[_row(7, vec_sim=0.8)])
    found = await retrieval.fetch_candidates(
        ts_query="аренда:*", acc_words=["itstep"], time_range=None, project=None,
        q_vec=[0.1, 0.2], limit=15)
    assert [r[0] for r in found.rows] == [1, 7]
    assert found.mode == "fts" and found.acc_words == ["itstep"]
    where, _params = fetch.await_args.args[2], fetch.await_args.args[3]
    assert "to_tsquery" not in where


@pytest.mark.parametrize(("column", "index", "q_vec"), [
    (False, False, [0.1]),   # миграция не накачена / SQLite
    (True, False, [0.1]),    # колонка есть, индекс ещё не построен (бэкфил идёт)
    (True, True, None),      # брокер эмбеддингов лёг
])
@pytest.mark.asyncio
async def test_no_ann_without_column_index_or_query_vector(monkeypatch, column, index, q_vec):
    """Без индекса ANN-запрос стал бы seq scan'ом всего корпуса на каждый поиск."""
    fetch = _patch_fetch(monkeypatch, column=column, index=index, semantic=[_row(9)])
    found = await retrieval.fetch_candidates(
        ts_query="x:*", acc_words=[], time_range=None, project=None,
        q_vec=q_vec, limit=15)
    assert [r[0] for r in found.rows] == [1]
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_sqlite_path_is_unchanged(sqlite_db):
    """Штатная ветка без колонки: JSONB-эмбеддинг в позиции 6, vec_sim нет."""
    from vera_shared.db.models import EventEmbeddingRow, EventRow

    now = datetime(2026, 9, 1)
    async with sqlite_db() as s:
        ev = EventRow(source="telegram", source_event_id="a", content_text="x",
                      occurred_at=now, received_at=now, triage_status="done")
        s.add(ev)
        await s.flush()
        s.add(EventEmbeddingRow(event_id=ev.id, embedding=[1.0, 0.0]))

    found = await retrieval.fetch_candidates(
        ts_query="", acc_words=[], time_range=None, project=None,
        q_vec=[1.0, 0.0], limit=15)
    assert found.mode == "vector"
    row = found.rows[0]
    emb = row[6] if isinstance(row[6], list) else json.loads(row[6])  # SQLite: текст
    assert emb == [1.0, 0.0]
    assert row_similarity(row, [1.0, 0.0]) == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_ann_index_unavailable_without_column(sqlite_db):
    vectors.forget_capability()
    assert await vectors.ann_index_available() is False


# ─── скоринг ────────────────────────────────────────────────────────────────


def test_db_similarity_wins_over_jsonb():
    row = _row(1, emb=[0.0, 1.0], vec_sim=0.75)
    assert row_similarity(row, [1.0, 0.0]) == 0.75


def test_unfilled_row_falls_back_to_jsonb_cosine():
    """Частично залитая колонка: строка без halfvec не теряет сходство."""
    row = _row(1, emb=[1.0, 0.0], vec_sim=None)
    assert row_similarity(row, [1.0, 0.0]) == pytest.approx(1.0)
    assert row_similarity(_row(2), [1.0, 0.0]) == 0.0
    assert row_similarity(row, None) == 0.0


def test_semantic_only_row_outranks_unrelated_fts_row():
    fts_hit = _row(1, rank=0.05)
    meaning = _row(2, vec_sim=0.8)
    ranked = score_rows([fts_hit, meaning], [0.1], [])
    assert [info["event_id"] for _s, info in ranked] == [2, 1]


# ─── запись и скрипт бэкфила ────────────────────────────────────────────────


def test_embedding_upsert_writes_every_available_column():
    stmt, params = vectors.embedding_upsert(5, [0.5, 0.25], True)
    assert "embedding_vec" in str(stmt) and "halfvec" in str(stmt)
    assert params == {"eid": 5, "emb": "[0.5, 0.25]", "vec": "[0.5,0.25]"}

    stmt, params = vectors.embedding_upsert(5, [0.5], False)
    assert "embedding_vec" not in str(stmt) and "vec" not in params


def _backfill():
    path = Path(__file__).resolve().parents[2] / "scripts" / "backfill_pgvector.py"
    spec = importlib.util.spec_from_file_location("backfill_pgvector", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_index_build_is_serial_and_memory_capped():
    """Параллельные воркеры строят граф в /dev/shm (256mb) — выключены;
    maintenance_work_mem ограничен, чтобы не упереться в mem_limit 768m."""
    stmts = _backfill().index_build_statements(1024, 192)
    assert stmts[0] == "SET maintenance_work_mem = '192MB'"
    assert stmts[1] == "SET max_parallel_maintenance_workers = 0"
    assert stmts[2] == vectors.ann_index_sql(1024)


def test_recall_at_k():
    recall_at_k = _backfill().recall_at_k
    assert recall_at_k([1, 2, 3, 4], [4, 3, 9, 8]) == 0.5
    assert recall_at_k([], [1]) == 1.0


def test_unfillable_rows_are_guarded_by_case_not_and():
    """AND в SQL не гарантирует порядок: jsonb_array_length на JSON null
    уронил бы всю порцию (на проде таких строк 2496)."""
    assert _backfill()._FILLABLE.startswith("CASE WHEN jsonb_typeof(embedding) = 'array'")


class _NestedSession:
    """Сессия, у которой смысловой запрос падает по таймауту."""

    def __init__(self, fail: bool):
        self.fail = fail
        self.nested_entered = 0
        self.calls = 0

    def begin_nested(self):
        session = self

        class _Ctx:
            async def __aenter__(self):
                session.nested_entered += 1
                return self

            async def __aexit__(self, *exc):
                return False       # исключение идёт наружу — savepoint откатится

        return _Ctx()

    async def execute(self, stmt, params=None):
        self.calls += 1
        if self.fail and self.calls == 2:     # 1-й — настройки, 2-й — сам ANN
            from sqlalchemy.exc import OperationalError
            raise OperationalError("SELECT …", {}, Exception("canceling statement due to statement timeout"))
        from unittest.mock import MagicMock
        res = MagicMock()
        res.all.return_value = [("row",)]
        return res


@pytest.mark.asyncio
async def test_ann_timeout_degrades_to_no_semantic_rows_instead_of_failing_search():
    """Ревью 13.09: без точки сохранения таймаут смыслового шага оборвал бы
    транзакцию, и поиск упал бы целиком вместо того, чтобы вернуть основную
    выборку."""
    from brain_search import ann
    s = _NestedSession(fail=True)
    rows = await ann.fetch_ann_rows(s, [0.1, 0.2, 0.3], "TRUE", {})
    assert rows == []
    assert s.nested_entered == 1


@pytest.mark.asyncio
async def test_ann_success_runs_inside_a_savepoint():
    from brain_search import ann
    s = _NestedSession(fail=False)
    rows = await ann.fetch_ann_rows(s, [0.1, 0.2, 0.3], "TRUE", {})
    assert rows == [("row",)]
    assert s.nested_entered == 1
