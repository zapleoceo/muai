"""Explicit event IDs must reach fresh legacy rows without FTS or vector lag."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from brain_search import retrieval
from brain_search.evidence import EVIDENCE_RULES
from brain_search.models import SearchQuery
from brain_search.pipeline import explicit_event_ids
from brain_search.rows import Candidate


def _event(event_id: int) -> Candidate:
    return Candidate(
        id=event_id, source="gmail", source_event_id=f"synthetic-{event_id}",
        occurred_at=datetime(2026, 10, 4, 18, tzinfo=timezone.utc),
        content_text="Synthetic account notice", importance=50,
        embedding=None, rank=1000.0, account="test-account",
    )


@pytest.mark.parametrize(("question", "expected"), [
    ("событие 900101", [900101]),
    ("события 900101/900102", [900101, 900102]),
    ("[event:900101]", [900101]),
    ("account 12345678901234 and payment 900101", []),
])
def test_only_explicit_event_references_become_ids(question, expected):
    assert explicit_event_ids(question) == expected


@pytest.mark.asyncio
async def test_exact_id_reads_base_events_without_search_index():
    class Session:
        def __init__(self):
            self.sql = ""
            self.params = {}

        async def execute(self, stmt, params):
            self.sql = str(stmt)
            self.params = params
            return SimpleNamespace(all=lambda: [_event(900102), _event(900101)])

    session = Session()
    rows = await retrieval._exact_rows(session, [900101, 900102], "gmail", None)
    assert [row.id for row in rows] == [900101, 900102]
    assert session.params["event_id_0"] == 900101
    assert session.params["event_id_1"] == 900102
    assert session.params["src"] == "gmail"
    assert "FROM events" in session.sql
    assert "event_embeddings" not in session.sql
    assert "to_tsvector" not in session.sql
    assert "hidden" in session.sql


@pytest.mark.asyncio
async def test_explicit_id_bypasses_stale_fts_and_embedding(monkeypatch):
    class Context:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return False

    primary = AsyncMock(side_effect=AssertionError("FTS must not run"))
    exact = AsyncMock(return_value=[_event(900101)])
    monkeypatch.setattr(retrieval, "get_session", Context)
    monkeypatch.setattr(retrieval, "_primary_with_degrade", primary)
    monkeypatch.setattr(retrieval, "_exact_rows", exact)
    found = await retrieval.fetch_candidates(
        ts_query="unrelated:*", acc_words=[], time_range=None, project=None,
        q_vec=None, limit=1, exact_event_ids=[900101, 900102],
    )
    assert found.mode == "exact_id"
    assert [row.id for row in found.rows] == [900101]
    exact.assert_awaited_once()
    assert exact.await_args.args[1] == [900101]
    primary.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_exact_id_is_not_reported_as_nonexistent(monkeypatch):
    from brain_search import app as search_app

    monkeypatch.setenv("INTERNAL_SECRET", "synthetic-test-secret")
    monkeypatch.setattr(search_app, "_try_report", AsyncMock(return_value=None))
    monkeypatch.setattr(search_app, "fetch_candidates", AsyncMock(
        return_value=retrieval.Candidates([], "exact_id")))
    embed = AsyncMock(side_effect=AssertionError("exact lookup needs no embedding"))
    synthesize = AsyncMock(side_effect=AssertionError("no evidence to synthesize"))
    monkeypatch.setattr(search_app, "embed_query", embed)
    monkeypatch.setattr(search_app, "synthesize", synthesize)
    response = await search_app.search(SearchQuery(q="событие 900101"),
                                       "synthetic-test-secret")
    assert response.results == []
    assert "среди доступных записей" in response.answer
    assert "не доказывает" in response.answer
    assert search_app.fetch_candidates.await_args.kwargs["exact_event_ids"] == [900101]
    embed.assert_not_awaited()
    synthesize.assert_not_awaited()


def test_sampled_search_does_not_license_absence_claim():
    from brain_search.agent import SYSTEM_PROMPT
    from brain_search.synthesis import build_prompt

    prompt = build_prompt(question="Did a new notice arrive?", self_ctx="",
                          context="(no selected candidates)", history_block="", notes="")
    assert "limited, possibly stale sample" in prompt
    assert "not returned by this search" in prompt
    assert EVIDENCE_RULES in SYSTEM_PROMPT
