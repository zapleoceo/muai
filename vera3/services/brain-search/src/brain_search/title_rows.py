"""Exact, explicitly labelled email-subject lookup alongside FTS."""
from __future__ import annotations

from sqlalchemy import text

from brain_search.retrieval_filters import semantic_filter
from brain_search.rows import META_COLUMNS, Candidate

TITLE_RANK = 100.0
TITLE_POOL = 30


async def fetch_title_rows(session, title: str, *, source: str | None,
                           links, time_range=None, project=None) -> list[Candidate]:
    """Find the stored subject exactly, inside the same visibility and link scope."""
    scope, params = semantic_filter(project, time_range, source, links)
    stmt = text(f"""
        SELECT id, source, source_event_id, occurred_at, content_text, importance,
               NULL AS embedding, {TITLE_RANK} AS rank, account, {META_COLUMNS}
        FROM events
        WHERE events.source = 'gmail' AND ({scope})
          AND (lower(metadata->>'subject') = :title
               OR (position(chr(10) || '---' in content_text) > 0
                   AND EXISTS (
                       SELECT 1 FROM unnest(string_to_array(
                           split_part(content_text, chr(10) || '---', 1),
                           chr(10))) AS header(line)
                       WHERE lower(header.line) = 'subject: ' || :title)))
        ORDER BY occurred_at DESC
        LIMIT {TITLE_POOL}
    """)
    rows = (await session.execute(stmt, {"title": title.lower(), **params})).all()
    return [Candidate.of(row) for row in rows]


def merge_title_rows(rows: list, titles: list[Candidate]) -> list:
    """Exact subject matches lead; retain vector similarity from a duplicate FTS row."""
    if not titles:
        return rows
    by_id = {candidate.id: candidate for candidate in titles}
    rest = []
    for row in rows:
        candidate = Candidate.of(row)
        if candidate.id in by_id:
            by_id[candidate.id] = by_id[candidate.id]._replace(vec_sim=candidate.vec_sim)
        else:
            rest.append(row)
    return list(by_id.values()) + rest
