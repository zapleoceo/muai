"""Synthetic regression for title/date recall and direct event lookup."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from brain_search import pipeline, retrieval, synthesis
from brain_search.models import AnswerResponse, SearchQuery
from brain_search.query_parse import explicit_title, parse_time_range, resolve_project
from brain_search.rows import Candidate
from brain_search.title_rows import TITLE_RANK, merge_title_rows


def _event(event_id: int, title: str, rank: float) -> Candidate:
    return Candidate(event_id, "gmail", f"synthetic-{event_id}",
                     datetime(2026, 10, 4, 12), f"Subject: {title}\n---\nSynthetic body",
                     50, None, rank, "synthetic-account")


@pytest.mark.parametrize(("question", "expected"), [
    ('Найди письмо с темой «Synthetic Sapphire Notice» от 4 октября',
     "Synthetic Sapphire Notice"),
    ('Find subject: "Synthetic Sapphire Notice" on 2026-10-04',
     "Synthetic Sapphire Notice"),
    ('Find title “Synthetic Sapphire Notice” on 2026-10-04',
     "Synthetic Sapphire Notice"),
    ('Find phrase "Synthetic Sapphire Notice" on 2026-10-04', None),
    ('Найди письмо с темой Synthetic Sapphire Notice', None),
])
def test_only_labelled_quoted_title_triggers_lookup(question, expected):
    assert explicit_title(question) == expected


def test_title_content_does_not_infer_project_or_date():
    q = 'Find subject "Veranda notice 2026-10-04" on 2026-10-05'
    assert resolve_project(q) is None
    assert parse_time_range(q, now_utc=datetime(2026, 10, 6)) == (
        datetime(2026, 10, 4, 17), datetime(2026, 10, 5, 17))
    assert resolve_project(q + " for project Itstep").name == "itstep"


@pytest.mark.asyncio
async def test_exact_title_survives_more_than_ten_noisy_fts_rows(monkeypatch):
    title = "Synthetic Sapphire Notice"
    noise = [_event(900100 + i, "Synthetic unrelated notice", 0.5)
             for i in range(15)]
    exact = _event(512322, title, TITLE_RANK)
    rows = merge_title_rows(noise, [exact])
    chat = AsyncMock(return_value=("Notice found [event:512322]", {"provider": "test"}))
    monkeypatch.setattr(synthesis, "self_context", AsyncMock(return_value=""))
    monkeypatch.setattr(synthesis, "chat_async", chat)
    response = await synthesis.answer(
        SearchQuery(q='Find subject: "Synthetic Sapphire Notice" on 2026-10-04',
                    use_agent=False), rows, None)
    assert response.results[0].event_id == 512322
    prompt = chat.await_args.kwargs["messages"][0]["content"]
    assert "[event:512322 |" in prompt


@pytest.mark.asyncio
async def test_candidate_pipeline_merges_exact_title_under_date_and_link_scope(monkeypatch):
    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    primary = AsyncMock(return_value=(retrieval.Candidates(
        [_event(900100, "Synthetic unrelated notice", 0.5)], "fts"), False))
    titles = AsyncMock(return_value=[_event(512322, "Synthetic Sapphire Notice", TITLE_RANK)])
    monkeypatch.setattr(retrieval, "get_session", Session)
    monkeypatch.setattr(retrieval, "_primary_with_degrade", primary)
    monkeypatch.setattr(retrieval, "fetch_title_rows", titles)
    window = (datetime(2026, 10, 3, 17), datetime(2026, 10, 4, 17))
    found = await retrieval.fetch_candidates(
        ts_query="unrelated:*", acc_words=[], time_range=window,
        project=SimpleNamespace(name="synthetic-project"),
        q_vec=None, limit=15, title="Synthetic Sapphire Notice")
    assert found.title_found and found.mode == "fts+title"
    assert [row.id for row in found.rows] == [512322, 900100]
    assert titles.await_args.kwargs["time_range"] == window
    assert titles.await_args.kwargs["project"].name == "synthetic-project"


@pytest.mark.asyncio
async def test_route_exact_id_is_not_hidden_by_inferred_date_or_project(monkeypatch):
    from brain_search import app as search_app

    monkeypatch.setenv("INTERNAL_SECRET", "synthetic-test-secret")
    fetch = AsyncMock(return_value=retrieval.Candidates(
        [_event(512322, "Synthetic Sapphire Notice", 1000)], "exact_id"))
    synthesis_call = AsyncMock(return_value=AnswerResponse(
        answer="Synthetic notice [event:512322]", results=[],
        provider="test", cost_usd=0))
    monkeypatch.setattr(search_app, "fetch_candidates", fetch)
    monkeypatch.setattr(search_app, "synthesize", synthesis_call)
    response = await search_app.search(
        SearchQuery(q="событие 512322 от 5 октября по проекту Веранда",
                    use_agent=False,
                    filters={"source": "gmail", "account": "synthetic-account"}),
        "synthetic-test-secret")
    assert "512322" in response.answer
    assert fetch.await_args.kwargs["exact_event_ids"] == [512322]
    assert fetch.await_args.kwargs["time_range"] is None
    assert fetch.await_args.kwargs["project"] is None
    assert fetch.await_args.kwargs["links"].flt.account == "synthetic-account"
    assert synthesis_call.await_args.kwargs["project"] is None


@pytest.mark.asyncio
async def test_missing_exact_subject_does_not_synthesize_noise_as_absence(monkeypatch):
    from brain_search import app as search_app

    monkeypatch.setenv("INTERNAL_SECRET", "synthetic-test-secret")
    monkeypatch.setattr(search_app, "embed_query", AsyncMock(return_value=None))
    monkeypatch.setattr(search_app, "fetch_candidates", AsyncMock(
        return_value=retrieval.Candidates(
            [_event(900100, "Synthetic unrelated notice", 0.5)],
            "fts+title", title_found=False)))
    synthesis_call = AsyncMock(side_effect=AssertionError("unrelated result must not answer"))
    monkeypatch.setattr(search_app, "synthesize", synthesis_call)
    result = await search_app.search(
        SearchQuery(q='Find subject: "Synthetic Sapphire Notice" on 2026-10-04'),
        "synthetic-test-secret")
    assert result.results == []
    assert "среди доступных записей" in result.answer
    assert "не доказывает" in result.answer
    synthesis_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_direct_id_ignores_inferred_window_but_keeps_source(monkeypatch):
    fetch = AsyncMock(return_value=retrieval.Candidates(
        [_event(512322, "Synthetic Sapphire Notice", 1000)], "exact_id"))
    monkeypatch.setattr(pipeline, "fetch_candidates", fetch)
    _, ranked = await pipeline.search_ranked(
        "event:512322", limit=5, source="gmail",
        time_range=(datetime(2026, 10, 5), datetime(2026, 10, 6)))
    assert ranked[0][1].id == 512322
    assert fetch.await_args.kwargs["time_range"] is None
    assert fetch.await_args.kwargs["source"] == "gmail"
