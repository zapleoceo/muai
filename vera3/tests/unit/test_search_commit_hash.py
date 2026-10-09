"""Хэши коммитов — точные идентификаторы наравне с тикетами."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from brain_search import app, pipeline
from brain_search.identifier_rows import (
    IDENTIFIER_RANK,
    fetch_identifier_rows,
    has_exact_rows,
    identifier_sql,
)
from brain_search.identifiers import (
    NO_EXACT_ANSWER,
    commit_hashes,
    exact_identifiers,
    identifier_hits,
    identifier_miss_note,
    identifier_note,
    is_identifier_only,
)
from brain_search.models import SearchQuery
from brain_search.retrieval import Candidates
from brain_search.rows import Candidate

_WHEN = datetime(2026, 10, 9, 9, tzinfo=timezone.utc)
_SHA = "4398a6d1f0c2b7e9a5d3c1b0e8f7a6d5c4b3a291"


def _cand(event_id: int, text: str, *, rank: float = 0.0) -> Candidate:
    return Candidate(id=event_id, source="gmail", source_event_id=f"s{event_id}",
                     occurred_at=_WHEN, content_text=text, importance=50,
                     embedding=None, rank=rank, account="a")


@pytest.mark.parametrize(("question", "expected"), [
    ("4398a6d", ["4398a6d"]),
    ("Deploy vera3 to Hetzner 4398A6D", ["4398a6d"]),
    (f"коммит {_SHA}", [_SHA]),
    ("12345678", []),            # только цифры
    ("deadbeef", []),            # только буквы a-f
    ("абвгде 123456 SIN-4885", []),
    ("4398a6", []),              # короче 7
    ("SIN-4885", []),
])
def test_commit_hashes(question: str, expected: list[str]):
    assert commit_hashes(question) == expected


def test_exact_identifiers_combines_tickets_and_hashes():
    assert exact_identifiers("SIN-4885 4398a6d") == ["SIN-4885", "4398a6d"]


def test_hash_hit_matches_whole_word_and_prefix_of_longer_hash():
    texts = {1: "failed Deploy vera3 4398a6d", 2: f"sha {_SHA}.", 3: "x14398a6d y",
             4: "43 98a6d"}
    assert identifier_hits(["4398a6d"], texts) == {"4398a6d": [1, 2]}


def test_identifier_only_detection():
    assert is_identifier_only("4398a6d", ["4398a6d"])
    assert is_identifier_only("SIN-4885", ["SIN-4885"])
    assert not is_identifier_only("Deploy vera3 to Hetzner 4398a6d", ["4398a6d"])
    assert not is_identifier_only("что со SIN-4885", ["SIN-4885"])
    assert not is_identifier_only("просто вопрос", [])


def test_miss_note_forbids_categorical_absence_claim():
    note = identifier_miss_note(["4398a6d"], {})
    assert "4398a6d" in note and "точных упоминаний не нашла" in note
    assert identifier_miss_note(["4398a6d"], {"4398a6d": [1]}) == ""
    assert "НЕЛЬЗЯ" in identifier_note({"4398a6d": [1]})


def test_hash_sql_uses_prefix_fts_and_hex_boundary():
    sql = identifier_sql(0, "TRUE", commit=True)
    assert "to_tsquery('russian', :p0)" in sql
    assert "phraseto_tsquery" not in sql
    assert "[^0-9a-f]" in sql and "[0-9a-f]{0,33}" in sql


@pytest.mark.asyncio
async def test_fetch_binds_hash_and_prefix_tsquery():
    result = MagicMock()
    result.all.return_value = [(510826, "gmail", "x", _WHEN, "4398a6d", 50, None,
                                IDENTIFIER_RANK, "a")]
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)
    rows = await fetch_identifier_rows(session, ["4398a6d"], source=None, links=None)
    _stmt, params = session.execute.await_args.args
    assert params["t0"] == "4398a6d" and params["p0"] == "4398a6d:*"
    assert [c.id for c in rows] == [510826]


@pytest.mark.asyncio
@pytest.mark.parametrize("question", ["4398a6d", "Deploy vera3 to Hetzner 4398a6d"])
async def test_exact_hash_hit_is_first(monkeypatch, question: str):
    exact = _cand(510826, "failed Deploy vera3 4398a6d", rank=IDENTIFIER_RANK)
    noise = [_cand(i, "deploy", rank=0.4) for i in range(1, 8)]

    async def fake_fetch(**kw):
        assert kw["tickets"] == ["4398a6d"]
        return Candidates(noise + [exact], "fts+ticket")

    monkeypatch.setattr(pipeline, "fetch_candidates", fake_fetch)
    monkeypatch.setattr(pipeline, "embed_query", AsyncMock(return_value=None))
    _found, ranked = await pipeline.search_ranked(question, limit=3)
    assert ranked[0][1].id == 510826


@pytest.mark.asyncio
async def test_identifier_only_without_exact_returns_no_rows(monkeypatch):
    noise = [_cand(i, "deploy", rank=0.4) for i in range(1, 8)]
    monkeypatch.setattr(pipeline, "fetch_candidates",
                        AsyncMock(return_value=Candidates(noise, "fts")))
    monkeypatch.setattr(pipeline, "embed_query", AsyncMock(return_value=None))
    _found, ranked = await pipeline.search_ranked("4398a6d", limit=5)
    assert ranked == [] and not has_exact_rows(noise)


@pytest.mark.asyncio
async def test_search_endpoint_identifier_only_miss_is_not_padded(monkeypatch):
    noise = [_cand(i, "deploy", rank=0.4) for i in range(1, 8)]
    monkeypatch.setattr(app, "check_internal_secret", lambda _s: None)
    monkeypatch.setattr(app, "_try_report", AsyncMock(return_value=None))
    monkeypatch.setattr(app, "embed_query", AsyncMock(return_value=None))
    monkeypatch.setattr(app, "fetch_candidates",
                        AsyncMock(return_value=Candidates(noise, "fts")))
    synth = AsyncMock()
    monkeypatch.setattr(app, "synthesize", synth)
    monkeypatch.setattr(app, "_link_scope", AsyncMock(return_value=None))
    resp = await app.search(SearchQuery(q="4398a6d", limit=5, use_agent=False), "s")
    assert resp.answer == NO_EXACT_ANSWER and resp.results == []
    assert "не нашла" in NO_EXACT_ANSWER and "не доказывает" in NO_EXACT_ANSWER
    synth.assert_not_awaited()
