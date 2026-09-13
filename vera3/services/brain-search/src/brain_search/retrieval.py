"""Выборка кандидатов из events — один запрос вместо шести копий.

В `search()` было шесть блоков `SELECT … FROM events LEFT JOIN
event_embeddings …`, различавшихся только WHERE и LIMIT (плюс седьмой,
почти такой же, в agent.py). Колонки в пяти из шести совпадали дословно.

Здесь одна форма запроса и явный набор режимов. Ветка «есть вектор, нет
слов» использует INNER JOIN намеренно: без эмбеддинга такая строка там
бесполезна, ранжировать нечем.

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

log = logging.getLogger(__name__)

#: Ширина выборки до скоринга. Скоринг переупорядочивает по косинусу и
#: весу источника, поэтому забираем заметно больше, чем отдадим.
CANDIDATE_POOL = 200
#: Ветка «ни слов, ни времени, ни вектора» — показать просто свежее.
RECENT_FALLBACK = 30

#: Разговоры с Верой — не «события мира». Системно по nature (её проставляет
#: триаж), source-фильтр остаётся для ещё не классифицированных.
_NOT_A_WORLD_EVENT = (" AND (nature IS NULL OR nature <> 'conversation_with_me')"
                      " AND source <> 'vera_chat'")

_BASE_COLUMNS = "id, source, source_event_id, occurred_at, content_text, importance"


@dataclass
class Candidates:
    """Строки + пояснение, какой веткой они получены (для логов и тестов)."""
    rows: list[Any]
    mode: str
    acc_words: list[str] = field(default_factory=list)


def _select(*, extra_cols: str, join: str, where: str, order: str,
            limit_sql: str, with_vec: bool = False) -> Any:
    # vec_sim — последней колонкой: scoring читает rank/account по позициям
    emb, sim = vec_columns() if with_vec else ("ee.embedding", "")
    return text(f"""
        SELECT {_BASE_COLUMNS}, {emb}, {extra_cols}{sim}
        FROM events
        {join} event_embeddings ee ON ee.event_id = events.id
        WHERE {where}
        ORDER BY {order}
        LIMIT {limit_sql}
    """)


def project_clause(project, time_range) -> tuple[str, dict[str, Any]]:
    """WHERE для проектной выборки: колонка `project` (её проставляет триаж по
    содержимому) ИЛИ реестр ящиков/чатов — fallback для неклассифицированных."""
    conds = ["(nature IS NULL OR nature NOT IN ('conversation_with_me', 'my_intent'))",
             "source <> 'vera_chat'"]
    params: dict[str, Any] = {"pname": project.name}
    ors = ["project = :pname"]
    for i, pat in enumerate(project.account_like):
        ors.append(f"account ILIKE :pacc{i}")
        params[f"pacc{i}"] = f"%{pat}%"
    if project.chats:
        ors.append("metadata->>'chat_title' = ANY(:pchats)")
        params["pchats"] = project.chats
    conds.append("(" + " OR ".join(ors) + ")")
    if time_range:
        conds.append("occurred_at >= :t_start AND occurred_at < :t_end")
        params["t_start"], params["t_end"] = time_range
    return " AND ".join(conds), params


def account_clause(acc_words: list[str]) -> tuple[str, str, dict[str, Any]]:
    """«Itstep» живёт в account='zaporozec_d@itstep.org', а письмо на
    английском текстовый FTS не найдёт. Возвращает (OR-хвост для WHERE,
    выражение для ORDER BY, параметры)."""
    if not acc_words:
        return "", "FALSE", {}
    ors, params = [], {}
    for i, w in enumerate(acc_words):
        ors.append(f"account ILIKE :acc{i}")
        params[f"acc{i}"] = f"%{w}%"
    joined = " OR ".join(ors)
    return " OR " + joined, "(" + joined + ")", params


def semantic_filter(project, time_range) -> tuple[str, dict[str, Any]]:
    """Фильтр для ANN-кандидатов: проект/окно/«не разговор с Верой» — всё,
    кроме текстового условия (его отсутствие и есть смысл ANN)."""
    if project is not None:
        return project_clause(project, time_range)
    if time_range:
        return (f"occurred_at >= :t_start AND occurred_at < :t_end{_NOT_A_WORLD_EVENT}",
                {"t_start": time_range[0], "t_end": time_range[1]})
    return f"TRUE{_NOT_A_WORLD_EVENT}", {}


async def fetch_candidates(
    *, ts_query: str, acc_words: list[str], time_range, project,
    q_vec: list[float] | None, limit: int,
) -> Candidates:
    """Кандидаты для скоринга: основной режим + смысловые из ANN."""
    with_vec = q_vec is not None and await vector_column_available()
    use_ann = with_vec and await ann_index_available()
    async with get_session() as s:
        found = await _primary(s, ts_query=ts_query, acc_words=acc_words,
                               time_range=time_range, project=project,
                               q_vec=q_vec, with_vec=with_vec, limit=limit)
        if use_ann and q_vec is not None:
            where, params = semantic_filter(project, time_range)
            semantic = await fetch_ann_rows(s, q_vec, where, params)
            before = len(found.rows)
            found.rows = merge_candidates(found.rows, semantic)
            log.info("retrieval=%s+ann: %d → %d", found.mode, before, len(found.rows))
    return found


async def _primary(s, *, ts_query: str, acc_words: list[str], time_range,
                   project, q_vec: list[float] | None, with_vec: bool,
                   limit: int) -> Candidates:
    """Режимы перечислены в порядке убывания точности."""
    vec_params: dict[str, Any] = {}
    if with_vec and q_vec is not None:
        vec_params = {"q": as_pg_vector(q_vec)}
    time_where = ""
    time_params: dict[str, Any] = {}
    if time_range:
        time_where = " AND occurred_at >= :t_start AND occurred_at < :t_end"
        time_params = {"t_start": time_range[0], "t_end": time_range[1]}
    plain = "0.0 AS rank, account"

    if project is not None:
        where, params = project_clause(project, time_range)
        stmt = _select(extra_cols=plain, join="LEFT JOIN", where=where,
                       order="occurred_at DESC", limit_sql=":lim", with_vec=with_vec)
        rows = (await s.execute(stmt, {**params, **vec_params, "lim": limit})).all()
        log.info("retrieval=project(%s): %d", project.name, len(rows))
        return Candidates(list(rows), "project")

    if ts_query:
        acc_where, acc_match, acc_params = account_clause(acc_words)
        stmt = _select(
            extra_cols="ts_rank(to_tsvector('russian', content_text),"
                       " to_tsquery('russian', :tsq)) AS rank,"
                       f" account, {acc_match} AS acc_match",
            join="LEFT JOIN",
            where=(f"(to_tsvector('russian', content_text)"
                   f" @@ to_tsquery('russian', :tsq){acc_where})"
                   f"{time_where}{_NOT_A_WORLD_EVENT}"),
            # acc_match первым: иначе account-совпадения с rank=0 (англ.
            # письма) отрезаются лимитом в пользу FTS-матчей.
            order="acc_match DESC, rank DESC, occurred_at DESC",
            limit_sql=str(CANDIDATE_POOL), with_vec=with_vec,
        )
        rows = (await s.execute(
            stmt, {"tsq": ts_query, **acc_params, **time_params, **vec_params})).all()
        if rows or not time_range:
            log.info("retrieval=fts: %d", len(rows))
            return Candidates(list(rows), "fts", acc_words)
        # FTS ничего не дал, но окно задано — отдадим всё окно

    if time_range:
        stmt = _select(extra_cols=plain, join="LEFT JOIN",
                       where=f"1=1{time_where}{_NOT_A_WORLD_EVENT}",
                       order="occurred_at DESC", limit_sql=str(CANDIDATE_POOL),
                       with_vec=with_vec)
        rows = (await s.execute(stmt, {**time_params, **vec_params})).all()
        log.info("retrieval=time: %d", len(rows))
        return Candidates(list(rows), "time")

    if q_vec is not None:
        stmt = _select(extra_cols=plain, join="JOIN",
                       where=f"1=1{_NOT_A_WORLD_EVENT}",
                       order="occurred_at DESC", limit_sql=str(CANDIDATE_POOL),
                       with_vec=with_vec)
        rows = (await s.execute(stmt, vec_params)).all()
        log.info("retrieval=vector: %d", len(rows))
        return Candidates(list(rows), "vector")

    stmt = _select(extra_cols=plain, join="LEFT JOIN",
                   where=f"1=1{_NOT_A_WORLD_EVENT}",
                   order="occurred_at DESC", limit_sql=str(RECENT_FALLBACK))
    rows = (await s.execute(stmt)).all()
    log.info("retrieval=recent: %d", len(rows))
    return Candidates(list(rows), "recent")
