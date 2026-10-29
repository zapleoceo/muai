"""Fetch bounded search candidates from events.

The primary path includes recent events before their embeddings exist. FTS,
project and time scopes use a left join for optional vector similarity; the
unscoped vector path also keeps unembedded recent rows. ANN adds older semantic
matches after the primary query. Search does not mutate stored events.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from vera_shared.db import vectors
from vera_shared.db.engine import get_session
from vera_shared.db.vectors import as_pg_vector

from brain_search.ann import fetch_ann_rows, merge_candidates, vec_sim_column
from brain_search.fts import fts_match_sql, fts_phrase_match_sql, fts_rank_sql
from brain_search.identifier_rows import (
    IDENTIFIER_RANK,
    fetch_identifier_rows,
    merge_identifier_rows,
)
from brain_search.retrieval_filters import (
    NOT_A_WORLD_EVENT,
    LinkScope,
    account_clause,
    links_clause,
    project_clause,
    semantic_filter,
    source_clause,
)
from brain_search.rows import META_COLUMNS, Candidate
from brain_search.title_rows import fetch_title_rows, merge_title_rows

__all__ = ["CANDIDATE_POOL", "RECENT_FALLBACK", "Candidates", "LinkScope", "account_clause",
           "fetch_candidates", "project_clause", "semantic_filter"]

log = logging.getLogger(__name__)

#: Ширина выборки до скоринга. Скоринг переупорядочивает по косинусу и
#: весу источника, поэтому забираем заметно больше, чем отдадим.
CANDIDATE_POOL = 200
#: Ветка «ни слов, ни времени, ни вектора» — показать просто свежее.
RECENT_FALLBACK = 30

_BASE_COLUMNS = ("id, source, source_event_id, occurred_at, "
                 "{content_expr} AS content_text, importance")
_PLAIN = "0.0 AS rank, account"


@dataclass
class Candidates:
    """Строки + пояснение, какой веткой они получены (для логов и тестов)."""
    rows: list[Any]
    mode: str
    acc_words: list[str] = field(default_factory=list)
    title_found: bool = False


def _select(*, extra_cols: str, join: str, where: str, order: str,
            limit_sql: str, with_vec: bool = False,
            content_expr: str = "content_text") -> Any:
    # первые девять колонок — по позициям (rows.Candidate), остальные по именам;
    # седьмая (embedding) зарезервирована и всегда NULL: вектор не покидает БД
    sim = vec_sim_column() if with_vec else ""
    join_sql = f"{join} event_embeddings ee ON ee.event_id = events.id" if with_vec else ""
    return text(f"""
        SELECT {_BASE_COLUMNS.format(content_expr=content_expr)}, NULL AS embedding,
               {extra_cols}{sim}, {META_COLUMNS}
        FROM events
        {join_sql}
        WHERE {where}
        ORDER BY {order}
        LIMIT {limit_sql}
    """)


async def _primary_with_degrade(s, **kw) -> tuple[Candidates, bool]:
    """(кандидаты, считался ли косинус в БД). Пока event_embeddings держит
    ACCESS EXCLUSIVE (VACUUM FULL из runbook), JOIN по ней ждёт блокировку;
    lock_timeout обрывает ожидание, и поиск идёт без вектора — на одном
    полнотексте, а не висит минутами."""
    if kw["q_vec"] is None:
        return await _primary(s, with_vec=False, **kw), False
    try:
        async with s.begin_nested():
            await s.execute(vectors.LOCK_TIMEOUT_SQL, vectors.lock_timeout_params())
            return await _primary(s, with_vec=True, **kw), True
    except DBAPIError as e:
        log.warning("retrieval: event_embeddings недоступна (%s) — поиск без вектора",
                    type(e.orig).__name__ if e.orig is not None else type(e).__name__)
    return await _primary(s, with_vec=False, **kw), False


async def fetch_candidates(
    *, ts_query: str, acc_words: list[str], time_range, project,
    q_vec: list[float] | None, limit: int, source: str | None = None,
    links: LinkScope | None = None,
    exact_event_ids: list[int] | None = None,
    tickets: list[str] | None = None,
    title: str | None = None,
    phrase: str | None = None,
) -> Candidates:
    """Кандидаты для скоринга: основной режим + смысловые из ANN."""
    async with get_session() as s:
        if exact_event_ids:
            rows = await _exact_rows(s, exact_event_ids[:limit], source, links,
                                     time_range=time_range, project=project)
            return Candidates([Candidate.of(row) for row in rows], "exact_id")
        if phrase:
            rows = await _phrase_rows(s, phrase, source, links,
                                      time_range=time_range, project=project)
            return Candidates([Candidate.of(row) for row in rows], "phrase")
        found, with_vec = await _primary_with_degrade(
            s, ts_query=ts_query, acc_words=acc_words, time_range=time_range,
            project=project, q_vec=q_vec, limit=limit, source=source, links=links)
        if with_vec and q_vec is not None:
            where, params = semantic_filter(project, time_range, source, links)
            semantic = await fetch_ann_rows(s, q_vec, where, params)
            before = len(found.rows)
            found.rows = merge_candidates(found.rows, semantic)
            log.info("retrieval=%s+ann: %d → %d", found.mode, before, len(found.rows))
        if tickets:
            exact = await fetch_identifier_rows(
                s, tickets, source=source, links=links,
                time_range=time_range, project=project)
            found.rows = merge_identifier_rows(found.rows, exact)
            found.mode += "+ticket"
        if title and time_range:
            exact_titles = await fetch_title_rows(
                s, title, source=source, links=links,
                time_range=time_range, project=project)
            found.rows = merge_title_rows(found.rows, exact_titles)
            found.title_found = bool(exact_titles)
            found.mode += "+title"
    found.rows = [Candidate.of(r) for r in found.rows]
    return found


async def _exact_rows(s, event_ids: list[int], source: str | None,
                      links: LinkScope | None, *, time_range=None, project=None) -> list[Any]:
    """Read named legacy events independently of FTS and embedding freshness."""
    scope_sql, scope_params = semantic_filter(project, time_range, source, links)
    binds = {f"event_id_{i}": event_id for i, event_id in enumerate(event_ids)}
    placeholders = ",".join(f":event_id_{i}" for i in range(len(event_ids)))
    stmt = _select(
        extra_cols="1000.0 AS rank, account", join="LEFT JOIN",
        where=f"events.id IN ({placeholders}) AND ({scope_sql})",
        order="events.id", limit_sql=str(len(event_ids)),
    )
    rows = list((await s.execute(stmt, binds | scope_params)).all())
    by_id = {Candidate.of(row).id: row for row in rows}
    return [by_id[event_id] for event_id in event_ids if event_id in by_id]


async def _phrase_rows(s, phrase: str, source: str | None,
                       links: LinkScope | None, *, time_range=None, project=None) -> list[Any]:
    """Return contiguous quoted matches within the same access and scope filters."""
    scope_sql, scope_params = semantic_filter(project, time_range, source, links)
    content_match = " OR ".join(
        f"to_tsvector('{cfg}', content_text) @@ phraseto_tsquery('{cfg}', :phrase)"
        for cfg in ("russian", "indonesian")
    )
    phrase_at = "strpos(lower(transcript_text), lower(:phrase))"
    transcript_excerpt = (
        f"CASE WHEN {phrase_at} > 0 THEN "
        f"substring(transcript_text FROM greatest({phrase_at} - 200, 1) "
        "FOR char_length(:phrase) + 400) "
        "WHEN to_tsvector('russian', transcript_text) "
        "@@ phraseto_tsquery('russian', :phrase) THEN "
        "ts_headline('russian', transcript_text, phraseto_tsquery('russian', :phrase), "
        "'MaxWords=80, MinWords=30, StartSel=[, StopSel=]') "
        "ELSE ts_headline('indonesian', transcript_text, "
        "phraseto_tsquery('indonesian', :phrase), "
        "'MaxWords=80, MinWords=30, StartSel=[, StopSel=]') END"
    )
    content_expr = (f"CASE WHEN ({content_match}) THEN content_text "
                    f"ELSE 'Transcript: ' || ({transcript_excerpt}) END")
    stmt = _select(
        extra_cols=f"{IDENTIFIER_RANK} AS rank, account", join="LEFT JOIN",
        where=f"({scope_sql}) AND {fts_phrase_match_sql('phrase')}",
        order="occurred_at DESC", limit_sql=str(CANDIDATE_POOL),
        content_expr=content_expr,
    )
    return list((await s.execute(stmt, {**scope_params, "phrase": phrase})).all())


async def _run(s, stmt, params: dict[str, Any], mode: str,
               acc_words: list[str] | None = None) -> Candidates:
    rows = list((await s.execute(stmt, params)).all())
    log.info("retrieval=%s: %d", mode, len(rows))
    return Candidates(rows, mode, acc_words or [])


async def _project_rows(s, *, ts_query: str, project, time_range, source,
                        vec_params: dict[str, Any], with_vec: bool,
                        limit: int, links: LinkScope | None = None) -> Candidates:
    """Слова запроса работают внутри проекта: FTS-ранг и ANN-добавка. Только
    когда содержательных слов нет (или они ничего не нашли) — свежее."""
    where, params = project_clause(project, time_range, source, links)
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
                   limit: int, source: str | None = None,
                   links: LinkScope | None = None) -> Candidates:
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
    link_sql, link_params = links_clause(links)
    src_sql, src_params = src_sql + link_sql, {**src_params, **link_params}
    world = NOT_A_WORLD_EVENT + src_sql

    if project is not None:
        return await _project_rows(s, ts_query=ts_query, project=project,
                                   time_range=time_range, source=source,
                                   vec_params=vec_params, with_vec=with_vec,
                                   limit=limit, links=links)

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
        # Newly received events can be searched before embedding finishes.
        # Keep them in the recent pool; ANN still adds older semantic matches.
        stmt = _select(extra_cols=_PLAIN, join="LEFT JOIN", where=f"1=1{world}",
                       order="occurred_at DESC", limit_sql=str(CANDIDATE_POOL),
                       with_vec=with_vec)
        return await _run(s, stmt, {**src_params, **vec_params}, "vector+recent")

    stmt = _select(extra_cols=_PLAIN, join="LEFT JOIN", where=f"1=1{world}",
                   order="occurred_at DESC", limit_sql=str(RECENT_FALLBACK))
    return await _run(s, stmt, src_params, "recent")
