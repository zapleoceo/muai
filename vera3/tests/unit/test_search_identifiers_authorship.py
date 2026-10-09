"""Exact ticket identifiers outrank semantic noise; authorship comes from metadata."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from brain_search import agent, agent_tools, pipeline
from brain_search.agent_tools import SearchEventsArgs
from brain_search.authorship import AUTHORSHIP_RULES, author_tag
from brain_search.identifier_rows import (
    IDENTIFIER_RANK,
    fetch_identifier_rows,
    merge_identifier_rows,
)
from brain_search.identifiers import (
    identifier_hits,
    identifier_note,
    ticket_ids,
)
from brain_search.models import SearchResult
from brain_search.retrieval import Candidates
from brain_search.rows import Candidate
from brain_search.scoring import score_candidates
from brain_search.synthesis import build_context, build_prompt
from vera_shared.events.visibility import NOT_HIDDEN_SQL

_WHEN = datetime(2026, 10, 9, 9, tzinfo=timezone.utc)


def _cand(event_id: int, text: str, *, rank: float = 0.0, vec_sim: float | None = None,
          **meta: str | None) -> Candidate:
    return Candidate(id=event_id, source="gmail", source_event_id=f"s{event_id}",
                     occurred_at=_WHEN, content_text=text, importance=50,
                     embedding=None, rank=rank, account="a", vec_sim=vec_sim, **meta)


@pytest.mark.parametrize(("question", "expected"), [
    ("SIN-4905", ["SIN-4905"]),
    ("что по sin-4905 и LAM-176?", ["SIN-4905", "LAM-176"]),
    ("SIN-4905 SIN-4905", ["SIN-4905"]),
    ("2026-10-09", []),
    ("e-mail", []),
    ("XSIN-4905-1", []),
    ("utf-8 gpt-4 covid-19 x-2 mp3-128 iso-8601 sha-256", []),
    ("AB-12 и A-12 и AB-1", ["AB-12"]),
])
def test_ticket_ids(question: str, expected: list[str]):
    assert ticket_ids(question) == expected


def test_exact_identifier_ranks_first_over_semantic_and_fts_noise():
    noise = [_cand(i, "sin payments", rank=0.35, vec_sim=0.8) for i in range(1, 11)]
    exact = [_cand(510716, "[Jira] (SIN-4905) Проверить ограничения", rank=IDENTIFIER_RANK)]
    merged = merge_identifier_rows(noise, exact)
    ranked = score_candidates(merged, [0.1], [])
    assert [c.id for _s, c in ranked[:1]] == [510716]
    assert ranked[0][0] > ranked[1][0] * 10


def test_merge_keeps_cosine_of_duplicate_and_drops_second_copy():
    primary = [_cand(7, "SIN-4905", rank=0.1, vec_sim=0.6), _cand(8, "other")]
    exact = [_cand(7, "SIN-4905", rank=IDENTIFIER_RANK)]
    merged = merge_identifier_rows(primary, exact)
    assert [Candidate.of(r).id for r in merged] == [7, 8]
    assert Candidate.of(merged[0]).vec_sim == 0.6
    assert Candidate.of(merged[0]).rank == IDENTIFIER_RANK


def test_merge_without_exact_rows_is_identity():
    rows = [_cand(1, "x")]
    assert merge_identifier_rows(rows, []) is rows


def test_no_absence_claim_when_lexical_match_exists():
    hits = identifier_hits(["SIN-4905", "ZZZ-1"], {
        510716: "[Jira] (SIN-4905) Проверить", 1: "SIN-49055 другое", 2: None})
    assert hits == {"SIN-4905": [510716]}
    note = identifier_note(hits)
    assert "[event:510716]" in note
    assert "НЕЛЬЗЯ отвечать" in note
    assert identifier_note({}) == ""


@pytest.mark.asyncio
async def test_fetch_identifier_rows_binds_raw_ticket_with_phraseto_and_boundary():
    result = MagicMock()
    result.all.return_value = [(510716, "gmail", "x", _WHEN, "SIN-4905", 50, None,
                                IDENTIFIER_RANK, "a")]
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)
    rows = await fetch_identifier_rows(session, ["SIN-4905"], source="gmail", links=None,
                                       time_range=(_WHEN, _WHEN))
    stmt, params = session.execute.await_args.args
    sql = str(stmt)
    assert params["t0"] == "SIN-4905"
    assert sql.count("phraseto_tsquery('russian', :t0)") == 2
    assert "phraseto_tsquery('indonesian', :t0)" in sql
    assert "<->" not in sql and "ILIKE" not in sql
    assert "[^[:alnum:]]" in sql
    assert [c.id for c in rows] == [510716]


@pytest.mark.asyncio
async def test_identifier_query_applies_hidden_source_and_period_filters():
    result = MagicMock()
    result.all.return_value = []
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)
    await fetch_identifier_rows(session, ["SIN-4905"], source="gmail", links=None,
                                time_range=(_WHEN, _WHEN))
    stmt, params = session.execute.await_args.args
    sql = str(stmt)
    assert NOT_HIDDEN_SQL in sql
    assert "source = :src" in sql
    assert "occurred_at >= :t_start" in sql
    assert params["src"] == "gmail" and params["t_start"] == _WHEN


@pytest.mark.asyncio
async def test_agent_search_path_passes_tickets_and_ranks_exact_first(monkeypatch):
    exact = _cand(510716, "SIN-4905", rank=IDENTIFIER_RANK)
    noise = [_cand(i, "sin", rank=0.35, vec_sim=0.8) for i in range(1, 8)]
    seen = {}

    async def fake_fetch(**kw):
        seen.update(kw)
        return Candidates(merge_identifier_rows(noise, [exact]), "fts+ticket")

    monkeypatch.setattr(pipeline, "fetch_candidates", fake_fetch)
    monkeypatch.setattr(pipeline, "embed_query", AsyncMock(return_value=None))
    _found, ranked = await pipeline.search_ranked("SIN-4905", limit=3)
    assert seen["tickets"] == ["SIN-4905"]
    assert ranked[0][1].id == 510716


@pytest.mark.asyncio
async def test_agent_tool_result_carries_identifier_note(monkeypatch):
    exact = _cand(510716, "[Jira] (SIN-4905) Проверить", rank=IDENTIFIER_RANK)
    monkeypatch.setattr(agent_tools, "search_ranked",
                        AsyncMock(return_value=(None, [(1.0, exact)])))
    res = await agent_tools._exec_search_events(SearchEventsArgs(q="SIN-4905"))
    assert "[event:510716]" in res["identifier_note"]
    obs = json.loads(agent._observation_text("search_events", res))
    assert "НЕЛЬЗЯ отвечать" in obs["identifier_note"]


def _gladkyi() -> Candidate:
    return _cand(510732, "Мерчант школы открытый вопрос. До решения поток 2 не включаем.",
                 author_role="counterparty", direction="received",
                 author_label='"Igor Gladkyi (JIRA)" <jira@itstep.atlassian.net>')


def _result(event_id: int) -> SearchResult:
    return SearchResult(score=1.0, event_id=event_id, source="gmail", source_url=None,
                        occurred_at="2026-10-09 09:00:00", content_preview="", importance=50)


def test_author_comes_from_metadata_not_mailbox_owner():
    c = _gladkyi()
    tag = author_tag(c)
    assert "Igor Gladkyi" in tag and "counterparty" in tag and "входящее" in tag
    context = build_context([_result(510732)], {510732: c})
    assert "author=" in context
    assert "Igor Gladkyi" in context
    assert "Дима" not in context


def test_outbound_self_event_is_attributed_to_owner():
    c = _cand(5, "Отправил", author_role="self", direction="sent")
    assert author_tag(c) == "author=Дима (self, исходящее)"


def test_event_without_metadata_has_no_author_tag():
    assert author_tag(_cand(6, "x")) == ""


def test_prompt_keeps_modality_and_authorship_rules_before_variable_data():
    context = build_context([_result(510732)], {510732: _gladkyi()})
    prompt = build_prompt(question="Q-UNIQUE", self_ctx="CFG", context=context,
                          history_block="", notes="\nNOTE-TAIL")
    assert AUTHORSHIP_RULES in prompt
    assert "не превращай в установленный факт" in prompt
    stable_end = prompt.index("### Твоя конфигурация")
    assert prompt.index(AUTHORSHIP_RULES) < stable_end
    assert stable_end < prompt.index("Q-UNIQUE") < prompt.index("[event:510732")
    assert prompt.rstrip().endswith("NOTE-TAIL")
    assert prompt.index("До решения поток 2 не включаем") > prompt.index("author=")


def test_candidate_of_reads_direction_by_name():
    row = SimpleNamespace(direction="received")
    c = Candidate.of((1, "gmail", "s", _WHEN, "t", 1, None, 0.0, "a"))
    assert c.direction is None
    assert Candidate.of(_tuple_with(row)).direction == "received"


def _tuple_with(ns: SimpleNamespace):
    class Row(tuple):
        direction = ns.direction

    return Row((1, "gmail", "s", _WHEN, "t", 1, None, 0.0, "a"))
