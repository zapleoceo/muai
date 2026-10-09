"""Общий путь «вопрос → кандидаты → скоринг» для /search и инструмента агента.

Раньше агентский search_events был второй самостоятельной реализацией
поиска: без ANN, без кусков, со своим стоп-листом. Теперь первая и
повторная выдача идут через одни и те же `fetch_candidates` и скоринг.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime

from vera_shared.llm.client import LLMCallFailed, embed

from brain_search.fts import build_ts_query
from brain_search.identifier_rows import has_exact_rows
from brain_search.identifiers import exact_identifiers, is_identifier_only
from brain_search.lang import content_words
from brain_search.query_parse import ProjectScope, extract_account_terms
from brain_search.quoted_query import split_quoted_query
from brain_search.retrieval import Candidates, LinkScope, fetch_candidates
from brain_search.rows import Candidate
from brain_search.scoring import score_candidates

log = logging.getLogger(__name__)

EMBED_TIMEOUT_S = 15
_EVENT_IDS = re.compile(
    r"\b(?:event|событи[а-я]*)\s*"
    r"(?:(?:id\s*[:#№]?\s*|[:#№]\s*)[0-9]{4,10}|[0-9]{5,10})(?![\w-])"
    r"(?:\s*(?:,|/|и\b|and\b)\s*[0-9]{5,10}(?![\w-])){0,4}",
    re.IGNORECASE,
)


def explicit_event_ids(question: str) -> list[int]:
    """Extract marked event IDs; four-digit IDs need punctuation or an ID marker.

    Otherwise a neighbouring year (or the start of an ISO date) becomes an ID.
    """
    _focus, scope = split_quoted_query(question)
    ids = [int(raw) for match in _EVENT_IDS.finditer(scope)
           for raw in re.findall(r"\d{4,10}", match.group())]
    return list(dict.fromkeys(ids))[:5]


def query_terms(question: str,
                project: ProjectScope | None = None) -> tuple[str, list[str]]:
    """(tsquery, имена собственные для матча по account). Слова самого
    проекта («Веранда») в запрос не идут: внутри проекта они есть в каждой
    строке и только размывают ранг."""
    focus, _scope = split_quoted_query(question)
    words = content_words(focus)
    if focus != question:
        return build_ts_query(words), []
    if project is not None:
        words = [w for w in words
                 if not any(t in w.lower() for t in project.triggers)]
    return build_ts_query(words), extract_account_terms(words)


async def embed_query(question: str) -> list[float] | None:
    """Вектор запроса. Отказ брокера не фатален — остаётся FTS."""
    try:
        focus, _scope = split_quoted_query(question)
        vecs = await asyncio.wait_for(embed([focus]), timeout=EMBED_TIMEOUT_S)
    except (LLMCallFailed, asyncio.TimeoutError) as e:
        log.warning("Embed failed: %s — fallback only FTS", e)
        return None
    return vecs[0] if vecs else None


async def search_ranked(
    question: str, *, limit: int, time_range: tuple[datetime, datetime] | None = None,
    source: str | None = None, project: ProjectScope | None = None,
    links: LinkScope | None = None,
) -> tuple[Candidates, list[tuple[float, Candidate]]]:
    """Полный проход поиска. Пустой вопрос = только окно времени, без вектора."""
    exact_ids = explicit_event_ids(question)
    identifiers = exact_identifiers(question)
    q_vec = await embed_query(question) if question.strip() and not exact_ids else None
    ts, acc_words = query_terms(question, project)
    found = await fetch_candidates(
        ts_query=ts, acc_words=acc_words, time_range=time_range, project=project,
        q_vec=q_vec, limit=limit, source=source, links=links,
        exact_event_ids=exact_ids, tickets=identifiers)
    if is_identifier_only(question, identifiers) and not has_exact_rows(found.rows):
        return found, []
    return found, score_candidates(found.rows, q_vec, found.acc_words)[:limit]
