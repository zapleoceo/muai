"""Quoted content must not become scope or an inferred account bonus."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from brain_search import pipeline
from brain_search.pipeline import explicit_event_ids, query_terms
from brain_search.query_parse import is_summary_query, parse_time_range, resolve_project
from brain_search.quoted_query import split_quoted_query
from brain_search.rows import Candidate
from brain_search.scoring import score_candidates
from vera_shared.links.filters import EventFilter

SHORT = 'Find phrase "amber otter lantern"'
LONG = ('Find messages in Synthetic Cedar chat on 2026-10-04; show correct author '
        'attribution for phrase "amber otter lantern"')


def test_quoted_relative_day_does_not_override_requested_date():
    question = 'Find quote "today amber lantern" on 2026-10-04'
    assert parse_time_range(question, now_utc=datetime(2026, 10, 6, 6)) == (
        datetime(2026, 10, 3, 17), datetime(2026, 10, 4, 17))


def test_project_in_quote_is_content_not_project_scope():
    assert resolve_project('Find phrase "Veranda amber lantern" on 2026-10-04') is None


def test_context_does_not_change_quote_lexical_search():
    assert query_terms(SHORT) == query_terms(LONG) == (
        "amber:* | otter:* | lantern:*", [])


def _candidate(event_id: int, *, body: str, account: str, importance: int) -> Candidate:
    return Candidate(id=event_id, source="telegram", source_event_id=f"synthetic-{event_id}",
                     occurred_at=datetime(2026, 10, 4, 12), content_text=body,
                     importance=importance, embedding=None, rank=0, account=account,
                     vec_sim=0.6)


@pytest.mark.parametrize("limit", [3, 10])
def test_context_account_bonus_cannot_drop_quote_from_same_semantic_pool(limit):
    # Isolate reranking: an ANN pool already includes the requested quote.
    # Equal cosine/FTS signals make the inferred account bonus observable.
    rows = [_candidate(900101, body="amber otter lantern", account="test-account", importance=50)]
    rows += [_candidate(900200 + i, body="unrelated notice", account="synthetic",
                        importance=25) for i in range(12)]
    ranked = {}
    for label, question in (("short", SHORT), ("long", LONG)):
        _, accounts = query_terms(question)
        ranked[label] = [row.id for _, row in score_candidates(rows, [1.0], accounts)[:limit]]
    assert ranked["short"][0] == 900101
    assert ranked["long"] == ranked["short"], ranked


@pytest.mark.asyncio
async def test_embedding_uses_quote_content_without_context_clauses(monkeypatch):
    embed = AsyncMock(return_value=[[0.6]])
    monkeypatch.setattr(pipeline, "embed", embed)
    assert await pipeline.embed_query(LONG) == [0.6]
    embed.assert_awaited_once_with(["amber otter lantern"])


@pytest.mark.parametrize("question", [
    'Find phrase "amber otter lantern"',
    'Найди фразу «amber otter lantern»',
    'Проверь цитату: “amber otter lantern”',
    '"amber otter lantern"',
])
def test_supported_single_quote_formats(question):
    focus, scope = split_quoted_query(question)
    assert focus == "amber otter lantern"
    assert "amber" not in scope


@pytest.mark.parametrize("question", [
    'Search chat "Synthetic Cedar"',
    'Compare quote "amber otter" with quote "silver fox"',
    'Find phrase "unclosed quotation',
    'Find phrase "   "',
    'Find phrase "' + "x" * 501 + '"',
    'Find phrase "escaped \\"quotation\\""',
])
def test_ambiguous_or_unsupported_quotes_keep_existing_query(question):
    assert split_quoted_query(question) == (question, question)


def test_scope_and_summary_intent_remain_outside_requested_quote():
    question = 'Find quote "today summary Veranda" for Itstep on 2026-10-04'
    assert resolve_project(question).name == "itstep"
    assert not is_summary_query(question)
    assert is_summary_query('Give a summary of quote "amber lantern"')


def test_project_words_inside_quote_remain_lexical_terms():
    question = 'Find phrase "Veranda amber lantern" for Itstep'
    ts, accounts = query_terms(question, resolve_project(question))
    assert ts == "Veranda:* | amber:* | lantern:*"
    assert accounts == []


def test_event_id_inside_requested_quote_is_not_a_direct_read_instruction():
    assert explicit_event_ids('Find quote "event:900101 amber lantern"') == []
    assert explicit_event_ids('Compare event:900102 with quote "event:900101 amber"') == [900102]


@pytest.mark.asyncio
async def test_quoted_report_words_do_not_intercept_search(monkeypatch):
    from brain_search import app as search_app

    find_chat = AsyncMock(side_effect=AssertionError("quoted report words are content"))
    monkeypatch.setattr(search_app, "find_report_chat", find_chat)
    assert await search_app._try_report('Найди цитату «отчёт помесячно за 2026 год»') is None
    find_chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_quote_search_keeps_structured_source_account_and_date_filters(monkeypatch):
    from brain_search import app as search_app
    from brain_search.models import AnswerResponse, SearchQuery
    from brain_search.retrieval import Candidates

    monkeypatch.setenv("INTERNAL_SECRET", "synthetic-test-secret")
    fetch = AsyncMock(return_value=Candidates([], "fts"))
    monkeypatch.setattr(search_app, "fetch_candidates", fetch)
    monkeypatch.setattr(search_app, "embed_query", AsyncMock(return_value=None))
    monkeypatch.setattr(search_app, "synthesize", AsyncMock(return_value=AnswerResponse(
        answer="Not returned by this search", results=[], provider=None, cost_usd=0)))
    filters = {"source": "gmail", "account": "test-account",
               "start": "2026-10-03T17:00:00", "end": "2026-10-04T17:00:00"}
    await search_app.search(SearchQuery(q='Find quote "today amber" on 2026-10-04',
                                         limit=3, use_agent=False, filters=filters),
                             "synthetic-test-secret")
    args = fetch.await_args.kwargs
    assert args["ts_query"] == "today:* | amber:*"
    assert args["acc_words"] == [] and args["limit"] == 3
    assert args["time_range"] == (datetime(2026, 10, 3, 17), datetime(2026, 10, 4, 17))
    assert args["links"].flt == EventFilter(source="gmail", account="test-account",
                                           start=datetime(2026, 10, 3, 17),
                                           end=datetime(2026, 10, 4, 17))


@pytest.mark.asyncio
async def test_agent_search_preserves_explicit_source_and_day_window(monkeypatch):
    from brain_search.agent_tools import SearchEventsArgs, _exec_search_events
    from brain_search.retrieval import Candidates

    fetch = AsyncMock(return_value=Candidates([], "fts"))
    embed = AsyncMock(return_value=[[1.0]])
    monkeypatch.setattr(pipeline, "fetch_candidates", fetch)
    monkeypatch.setattr(pipeline, "embed", embed)
    await _exec_search_events(SearchEventsArgs(q=LONG, source="gmail", limit=3,
                                               date_from="2026-10-04", date_to="2026-10-04"))
    args = fetch.await_args.kwargs
    assert args["source"] == "gmail" and args["limit"] == 3
    assert args["time_range"] == (datetime(2026, 10, 3, 17), datetime(2026, 10, 4, 17))
    assert args["ts_query"] == "amber:* | otter:* | lantern:*"
    embed.assert_awaited_once_with(["amber otter lantern"])


@pytest.mark.parametrize("use_agent", [False, True])
@pytest.mark.asyncio
async def test_empty_quote_selection_does_not_claim_source_absence(monkeypatch, use_agent):
    from brain_search import app as search_app
    from brain_search.models import AnswerResponse, SearchQuery
    from brain_search.retrieval import Candidates

    monkeypatch.setenv("INTERNAL_SECRET", "synthetic-test-secret")
    monkeypatch.setattr(search_app, "fetch_candidates", AsyncMock(return_value=Candidates([], "fts")))
    monkeypatch.setattr(search_app, "embed_query", AsyncMock(return_value=None))
    synthesis = AsyncMock(return_value=AnswerResponse(answer="Agent may search further",
                                                     results=[], provider=None, cost_usd=0))
    monkeypatch.setattr(search_app, "synthesize", synthesis)
    result = await search_app.search(SearchQuery(q=SHORT, use_agent=use_agent),
                                     "synthetic-test-secret")
    if use_agent:
        synthesis.assert_awaited_once()
    else:
        synthesis.assert_not_awaited()
        assert result.results == [] and result.provider is None and result.cost_usd == 0
        assert "ограниченная поисковая выборка" in result.answer
        assert "не доказывает отсутствие" in result.answer
