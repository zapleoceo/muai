"""Точная выборка событий по тикет-идентификатору, независимо от OR-запроса."""
from __future__ import annotations

from typing import Any

from sqlalchemy import text

from brain_search.fts import fts_phrase_match_sql
from brain_search.retrieval_filters import semantic_filter
from brain_search.rows import META_COLUMNS, Candidate

#: Выше любого ts_rank×2 + косинус, но ниже 1000 прямого запроса по event id.
IDENTIFIER_RANK = 100.0
PER_IDENTIFIER_LIMIT = 30
#: Граница по не-буквоцифре: XSIN-4905 и SIN-49055 не считаются совпадением.
_BOUNDARY = "'(^|[^[:alnum:]])' || {p} || '($|[^[:alnum:]])'"


def identifier_sql(i: int, scope_sql: str) -> str:
    """Фраза по GIN-индексам + проверка границ regex'ом; `:t{i}` — сам тикет."""
    boundary = _BOUNDARY.format(p=f":t{i}")
    return f"""
        SELECT id, source, source_event_id, occurred_at, content_text, importance,
               NULL AS embedding, {IDENTIFIER_RANK} AS rank, account, {META_COLUMNS}
        FROM events
        WHERE {fts_phrase_match_sql(f"t{i}")}
          AND (content_text ~* ({boundary}) OR transcript_text ~* ({boundary}))
          AND ({scope_sql})
        ORDER BY occurred_at DESC
        LIMIT {PER_IDENTIFIER_LIMIT}
    """


async def fetch_identifier_rows(s, tickets: list[str], *, source: str | None,
                                links, time_range=None, project=None) -> list[Candidate]:
    """События, где идентификатор встречается дословно."""
    scope_sql, scope_params = semantic_filter(project, time_range, source, links)
    out: dict[int, Candidate] = {}
    for i, ticket in enumerate(tickets):
        # ticket состоит только из [A-Za-z0-9-] (identifiers.ticket_ids): в regex безопасен
        params: dict[str, Any] = {f"t{i}": ticket, **scope_params}
        for row in (await s.execute(text(identifier_sql(i, scope_sql)), params)).all():
            c = Candidate.of(row)
            out.setdefault(c.id, c)
    return list(out.values())


def merge_identifier_rows(rows: list[Any], exact: list[Candidate]) -> list[Any]:
    """Точные совпадения первыми; у дубля из основной выборки сохраняется косинус."""
    if not exact:
        return rows
    by_id = {c.id: c for c in exact}
    rest: list[Any] = []
    for r in rows:
        c = Candidate.of(r)
        if c.id in by_id:
            by_id[c.id] = by_id[c.id]._replace(vec_sim=c.vec_sim)
        else:
            rest.append(r)
    return list(by_id.values()) + rest
