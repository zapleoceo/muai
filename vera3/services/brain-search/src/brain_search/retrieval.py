"""Выборка кандидатов из events — один запрос вместо шести копий.

В `search()` было шесть блоков `SELECT … FROM events LEFT JOIN
event_embeddings …`, различавшихся только WHERE и LIMIT (плюс седьмой,
почти такой же, в agent.py). Колонки в пяти из шести совпадали дословно.

Здесь одна форма запроса и явный набор режимов. Ветка «есть вектор, нет
слов» использует INNER JOIN намеренно: без эмбеддинга такая строка там
бесполезна, ранжировать нечем. Агентский search_events идёт через тот же
`fetch_candidates`, поэтому повторный поиск не отличается от первого.

Поверх любого режима, когда есть вектор запроса и ANN-индекс, добавляются
смысловые кандидаты из всего корпуса (ann.py) с тем же фильтром.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from vera_shared.db.engine import get_session
from vera_shared.db.vectors import (
    ann_index_available,
    as_pg_vector,
    vector_column_available,
)

from brain_search.ann import fetch_ann_rows, merge_candidates, vec_columns
from brain_search.fts import fts_match_sql, fts_rank_sql
from brain_search.retrieval_filters import (
    NOT_A_WORLD_EVENT,
    account_clause,
    project_clause,
    semantic_filter,
    source_clause,
)
from brain_search.rows import META_COLUMNS, Candidate

__all__ = ["CANDIDATE_POOL", "RECENT_FALLBACK", "Candidates", "account_clause",
           "fetch_candidates", "project_clause", "semantic_filter"]

log = logging.getLogger(__name__)

#: Ширина выборки до скоринга. Скоринг переупорядочивает по косинусу и
#: весу источника, поэтому забираем заметно больше, чем отдадим.
CANDIDATE_POOL = 200
#: Ветка «ни слов, ни времени, ни вектора» — показать просто свежее.
RECENT_FALLBACK = 30

_BASE_COLUMNS = "id, source, source_event_id, occurred_at, content_text, importance"
_PLAIN = "0.0 AS rank, account"


@dataclass
class Candidates:
    """Строки + пояснение, какой веткой они получены (для логов и тестов)."""
    rows: list[Any]
    mode: str
    acc_words: list[str] = field(default_factory=list)


def _select(*, extra_cols: str, join: str, where: str, order: str,
            limit_sql: str, with_vec: bool = False) -> Any:
    # первые девять колонок — по позициям (rows.Candidate), остальные по именам
    emb, sim = vec_columns() if with_vec else ("ee.embedding", "")
    return text(f"""
        SELECT {_BASE_COLUMNS}, {emb}, {extra_cols}{sim}, {META_COLUMNS}
        FROM events
        {join} event_embeddings ee ON ee.event_id = events.id
        WHERE {where}
        ORDER BY {order}
        LIMIT {limit_sql}
    """)


async def fetch_candidates(
    *, ts_query: str, acc_words: list[str], time_range, project,
    q_vec: list[float] | None, limit: int, source: str | None = None,
) -> Candidates:
    """Кандидаты для скоринга: основной режим + смысловые из ANN."""
    with_vec = q_vec is not None and await vector_column_available()
    use_ann = with_vec and await ann_index_available()
    async with get_session() as s:
        found = await _primary(s, ts_query=ts_query, acc_words=acc_words,
                               time_range=time_range, project=project,
                               q_vec=q_vec, with_vec=with_vec, limit=limit,
                               source=source)
        if use_ann and q_vec is not None:
            where, params = semantic_filter(project, time_range, source)
            semantic = await fetch_ann_rows(s, q_vec, where, params)
            before = len(found.rows)
            found.rows = merge_candidates(found.rows, semantic)
            log.info("retrieval=%s+ann: %d → %d", found.mode, before, len(found.rows))
    found.rows = [Candidate.of(r) for r in found.rows]
    return found


async def _run(s, stmt, params: dict[str, Any], mode: str,
               acc_words: list[str] | None = None) -> Candidates:
    rows = list((await s.execute(stmt, params)).all())
    log.info("retrieval=%s: %d", mode, len(rows))
    return Candidates(rows, mode, acc_words or [])


async def _project_rows(s, *, ts_query: str, project, time_range, source,
                        vec_params: dict[str, Any], with_vec: bool,
                        limit: int) -> Candidates:
    """Слова запроса работают внутри проекта: FTS-ранг и ANN-добавка. Только
    когда содержательных слов нет (или они ничего не нашли) — свежее."""
    where, params = project_clause(project, time_range, source)
    if ts_query:
        stmt = _select(extra_cols=f"{fts_rank_sql()} AS rank, account",
                       join="LEFT JOIN", where=f"{where} AND {fts_match_sql()}",
                       order="rank DESC, occurred_at DESC",
                       limit_sql=str(CANDIDATE_POOL), with_vec=with_vec)
        found = await _run(s, stmt, {**params, **vec_params, "tsq": ts_query},
                           f"project({project.name})+fts")
        if found.rows:
            return found
    stmt = _select(extra_cols=_PLAIN, join="LEFT JOIN", where=where,
                   order="occurred_at DESC", limit_sql=":lim", with_vec=with_vec)
    return await _run(s, stmt, {**params, **vec_params, "lim": max(limit, RECENT_FALLBACK)},
                      f"project({project.name})+recent")


async def _primary(s, *, ts_query: str, acc_words: list[str], time_range,
                   project, q_vec: list[float] | None, with_vec: bool,
                   limit: int, source: str | None = None) -> Candidates:
    """Режимы перечислены в порядке убывания точности."""
    vec_params: dict[str, Any] = {}
    if with_vec and q_vec is not None:
        vec_params = {"q": as_pg_vector(q_vec)}
    time_where = ""
    time_params: dict[str, Any] = {}
    if time_range:
        time_where = " AND occurred_at >= :t_start AND occurred_at < :t_end"
        time_params = {"t_start": time_range[0], "t_end": time_range[1]}
    src_sql, src_params = source_clause(source)
    world = NOT_A_WORLD_EVENT + src_sql

    if project is not None:
        return await _project_rows(s, ts_query=ts_query, project=project,
                                   time_range=time_range, source=source,
                                   vec_params=vec_params, with_vec=with_vec,
                                   limit=limit)

    if ts_query:
        acc_where, acc_match, acc_params = account_clause(acc_words)
        stmt = _select(
            extra_cols=f"{fts_rank_sql()} AS rank, account, {acc_match} AS acc_match",
            join="LEFT JOIN",
            where=f"({fts_match_sql()}{acc_where}){time_where}{world}",
            # acc_match первым: иначе account-совпадения с rank=0 (англ.
            # письма) отрезаются лимитом в пользу FTS-матчей.
            order="acc_match DESC, rank DESC, occurred_at DESC",
            limit_sql=str(CANDIDATE_POOL), with_vec=with_vec,
        )
        found = await _run(s, stmt, {"tsq": ts_query, **acc_params, **time_params,
                                     **src_params, **vec_params}, "fts", acc_words)
        if found.rows or not time_range:
            return found
        # FTS ничего не дал, но окно задано — отдадим всё окно

    if time_range:
        stmt = _select(extra_cols=_PLAIN, join="LEFT JOIN",
                       where=f"1=1{time_where}{world}", order="occurred_at DESC",
                       limit_sql=str(CANDIDATE_POOL), with_vec=with_vec)
        return await _run(s, stmt, {**time_params, **src_params, **vec_params}, "time")

    if q_vec is not None:
        stmt = _select(extra_cols=_PLAIN, join="JOIN", where=f"1=1{world}",
                       order="occurred_at DESC", limit_sql=str(CANDIDATE_POOL),
                       with_vec=with_vec)
        return await _run(s, stmt, {**src_params, **vec_params}, "vector")

    stmt = _select(extra_cols=_PLAIN, join="LEFT JOIN", where=f"1=1{world}",
                   order="occurred_at DESC", limit_sql=str(RECENT_FALLBACK))
    return await _run(s, stmt, src_params, "recent")
