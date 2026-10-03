"""Качество поиска brain-search: язык запроса, проектная выборка, вес
источников и авторов-ботов, безопасность инструментов агента."""
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import brain_search.agent as agent
import pytest
from brain_search import agent_tools, retrieval
from brain_search.lang import content_words, is_stopword
from brain_search.models import SearchQuery
from brain_search.pipeline import query_terms
from brain_search.query_parse import (
    BOT_AUTHOR_WEIGHT,
    parse_time_range,
    resolve_project,
    source_weight,
)
from brain_search.retrieval_filters import (
    project_clause,
    semantic_filter,
    source_clause,
)
from brain_search.rows import Candidate
from brain_search.scoring import score_candidates, score_rows
from pydantic import ValidationError

NOW = datetime(2026, 10, 3, 5, 0)


# ─── язык ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("word", ["bagaimana", "який", "who", "what", "кто",
                                  "сколько", "yang", "WHERE", "скільки"])
def test_question_words_of_every_language_are_stopwords(word):
    assert is_stopword(word)


def test_content_words_keep_meaning_across_languages():
    assert content_words("bagaimana status pembayaran siswa") == ["status", "pembayaran", "siswa"]
    assert content_words("який графік занять у нових груп") == ["графік", "занять", "нових", "груп"]
    assert content_words("who is responsible for CRM reports") == ["responsible", "CRM", "reports"]
    assert content_words("а б Иван") == ["Иван"]


def test_project_trigger_words_leave_the_query():
    question = "договор с поставщиком кальянов Веранда"
    ts, _ = query_terms(question, resolve_project(question))
    assert "Веранда" not in ts and "договор:*" in ts
    ts_all, _ = query_terms("договор Веранда")
    assert "Веранда:*" in ts_all


# ─── даты ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("q", ["бюджет 3.5", "вышло 1.5 млн", "версия v1.2.3",
                               "цена 12.50 usd", "1.5 juta"])
def test_amounts_and_versions_are_not_dates(q):
    assert parse_time_range(q, now_utc=NOW) is None


def test_two_digit_dotted_date_still_parses():
    assert parse_time_range("отчёт 09.06", now_utc=NOW) is not None
    assert parse_time_range("отчёт 9.6.2026", now_utc=NOW) is not None


@pytest.mark.parametrize(("q", "days_ago"), [
    ("yesterday invoices", 1), ("kemarin dulu", 2), ("вчора листи", 1),
    ("today", 0), ("hari ini", 0), ("сьогодні", 0), ("позавчора", 2),
    ("day before yesterday", 2),
])
def test_relative_days_in_every_language(q, days_ago):
    start, end = parse_time_range(q, now_utc=NOW)
    today_start, _ = parse_time_range("сегодня", now_utc=NOW)
    assert (today_start - start).days == days_ago
    assert (end - start).days == 1


# ─── строки и скоринг ───────────────────────────────────────────────────────


def _cand(eid, **kw):
    base = {"id": eid, "source": "gmail", "source_event_id": f"s{eid}",
            "occurred_at": NOW, "content_text": "t", "importance": None,
            "embedding": None, "rank": 0.0, "account": ""}
    return Candidate(**{**base, **kw})


def test_candidate_of_legacy_tuple_and_named_attrs():
    legacy = (1, "gmail", "s1", NOW, "t", 5, None, 0.3, "a@b")
    c = Candidate.of(legacy)
    assert (c.rank, c.account, c.vec_sim, c.is_bot) == (0.3, "a@b", None, False)
    assert Candidate.of(c) is c
    short = Candidate.of((1, "gmail", "s1", NOW, "t", 5))
    assert short.rank is None and short.account is None


def test_vera_memory_has_no_boost_over_primary_events():
    assert source_weight("vera_memory") == 1.0
    memory = _cand(1, source="vera_memory", rank=0.1, importance=60)
    event = _cand(2, source="telegram", rank=0.1, importance=60)
    scores = {c.id: s for s, c in score_candidates([memory, event], None, [])}
    assert scores[1] == pytest.approx(scores[2])


def test_bot_authored_event_is_downweighted_not_dropped():
    human = _cand(1, rank=0.2)
    bot = _cand(2, rank=0.2, is_bot=True)
    ranked = score_candidates([bot, human], None, [])
    assert [c.id for _s, c in ranked] == [1, 2]
    assert ranked[1][0] == pytest.approx(ranked[0][0] * BOT_AUTHOR_WEIGHT)


def test_score_rows_preview_shape_unchanged():
    ((_score, info),) = score_rows([_cand(7, content_text="x" * 500)], None, [])
    assert set(info) == {"event_id", "source", "occurred_at", "content_preview", "importance"}
    assert len(info["content_preview"]) == 400


# ─── фильтры и проектная выборка ────────────────────────────────────────────


def test_source_clause():
    assert source_clause(None) == ("", {})
    assert source_clause("any") == ("", {})
    assert source_clause("gmail") == (" AND source = :src", {"src": "gmail"})


def test_source_reaches_project_and_semantic_filters():
    project = SimpleNamespace(name="p")
    where, params = project_clause(project, None, "gmail")
    assert "source = :src" in where and params["src"] == "gmail"
    where, params = semantic_filter(None, (NOW, NOW), "telegram")
    assert "source = :src" in where and params["src"] == "telegram"
    where, params = semantic_filter(None, None, "gmail")
    assert where.startswith("TRUE") and params == {"src": "gmail"}


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Session:
    def __init__(self, results):
        self.results = list(results)
        self.sql: list[str] = []
        self.params: list[dict] = []

    async def execute(self, stmt, params=None):
        self.sql.append(str(stmt))
        self.params.append(params or {})
        return _Result(self.results.pop(0) if self.results else [])


PROJECT = SimpleNamespace(name="veranda")


@pytest.mark.asyncio
async def test_project_mode_applies_query_words_with_rank_order():
    s = _Session([[_cand(1)]])
    found = await retrieval._primary(
        s, ts_query="договор:*", acc_words=[], time_range=None, project=PROJECT,
        q_vec=None, with_vec=False, limit=15)
    assert found.mode == "project(veranda)+fts"
    assert "to_tsquery" in s.sql[0] and "project = :pname" in s.sql[0]
    assert "ORDER BY rank DESC" in s.sql[0]
    assert s.params[0]["tsq"] == "договор:*"


@pytest.mark.asyncio
async def test_project_mode_falls_back_to_recency_without_words_or_matches():
    s = _Session([])
    found = await retrieval._primary(
        s, ts_query="", acc_words=[], time_range=None, project=PROJECT,
        q_vec=None, with_vec=False, limit=60)
    assert found.mode == "project(veranda)+recent"
    assert "to_tsquery" not in s.sql[0] and s.params[0]["lim"] == 60

    s = _Session([[], [_cand(2)]])
    found = await retrieval._primary(
        s, ts_query="нет:*", acc_words=[], time_range=None, project=PROJECT,
        q_vec=None, with_vec=False, limit=15)
    assert found.mode.endswith("+recent") and len(s.sql) == 2
    assert s.params[1]["lim"] == retrieval.RECENT_FALLBACK


@pytest.mark.asyncio
async def test_source_filter_goes_into_fts_sql():
    s = _Session([[_cand(1)]])
    await retrieval._primary(
        s, ts_query="x:*", acc_words=[], time_range=None, project=None,
        q_vec=None, with_vec=False, limit=15, source="gmail")
    assert "source = :src" in s.sql[0] and s.params[0]["src"] == "gmail"


def test_select_exposes_bot_flag_and_author_columns():
    sql = str(retrieval._select(extra_cols="0.0 AS rank, account", join="JOIN",
                                where="TRUE", order="id", limit_sql="5"))
    for name in ("AS is_bot", "AS author_role", "AS author_label", "AS chat_title"):
        assert name in sql


# ─── модели ─────────────────────────────────────────────────────────────────


def test_search_query_bounds():
    assert SearchQuery(q="x").limit == 15
    with pytest.raises(ValidationError):
        SearchQuery(q="x", limit=100000)
    with pytest.raises(ValidationError):
        SearchQuery(q="x", max_steps=50)
    with pytest.raises(ValidationError):
        SearchQuery(q="x", limit=0)


# ─── безопасность инструментов агента ───────────────────────────────────────


def test_search_args_bounds_and_unknown_keys_ignored():
    args = agent_tools.SearchEventsArgs.model_validate(
        {"q": "x", "limit": 7, "evil": "drop table"})
    assert args.limit == 7 and not hasattr(args, "evil")
    with pytest.raises(ValidationError):
        agent_tools.SearchEventsArgs.model_validate({"q": "x", "limit": 5000})
    with pytest.raises(ValidationError):
        agent_tools.SearchEventsArgs.model_validate({"source": "../etc"})


def test_remember_args_bounds():
    with pytest.raises(ValidationError):
        agent_tools.RememberArgs.model_validate({"fact": ""})
    with pytest.raises(ValidationError):
        agent_tools.RememberArgs.model_validate({"fact": "x", "confidence": 7})
    with pytest.raises(ValidationError):
        agent_tools.RememberArgs.model_validate({"fact": "x", "tags": ["t"] * 11})


def test_date_window_uses_local_days_inclusive():
    assert agent_tools.date_window(None, None) is None
    start, end = agent_tools.date_window("2026-06-09", "2026-06-09")
    assert (end - start).days == 1
    open_ended = agent_tools.date_window("2026-06-09", None)
    assert open_ended[1] > open_ended[0]
    assert agent_tools.parse_iso_date("мусор") is None


TOOL = agent_tools.ToolDescriptor("search_events", "d", {}, "builtin:search_events")
MEMORY = agent_tools.ToolDescriptor("memory.remember", "d", {}, "builtin:memory")


@pytest.mark.asyncio
async def test_execute_tool_returns_error_observations_never_raises():
    assert "error" in await agent_tools.execute_tool(TOOL, ["not", "a", "dict"])
    bad = await agent_tools.execute_tool(TOOL, {"limit": "много"})
    assert bad["error"] == "invalid params" and bad["details"]
    json.dumps(bad)

    with patch.object(agent_tools, "search_ranked", AsyncMock(side_effect=RuntimeError("db"))):
        obs = await agent_tools.execute_tool(TOOL, {"q": "x"})
    assert obs["error"].startswith("RuntimeError")

    unknown = agent_tools.ToolDescriptor("x", "d", {}, "weird")
    assert "no invoker" in (await agent_tools.execute_tool(unknown, {}))["error"]


@pytest.mark.asyncio
async def test_agent_search_goes_through_shared_pipeline():
    ranked = [(1.0, _cand(5, author_role="self", chat_title="Chat", content_text="привет"))]
    with patch.object(agent_tools, "search_ranked",
                      AsyncMock(return_value=(None, ranked))) as call:
        obs = await agent_tools.execute_tool(
            TOOL, {"q": "аренда", "source": "gmail", "limit": 3,
                   "date_from": "2026-06-09"})
    kwargs = call.await_args.kwargs
    assert kwargs["limit"] == 3 and kwargs["source"] == "gmail"
    assert kwargs["time_range"] is not None
    assert obs["found"] == 1 and obs["events"][0]["author_role"] == "self"


class _MemSession:
    def __init__(self):
        self.added = []

    def add(self, row):
        row.id = 99
        self.added.append(row)

    async def flush(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_agent_memory_carries_provenance():
    s = _MemSession()
    with patch.object(agent_tools, "get_session", lambda: s):
        obs = await agent_tools.execute_tool(
            MEMORY, {"fact": "X", "tags": ["a"], "confidence": 0.5})
    assert obs == {"saved": True, "event_id": 99}
    meta = s.added[0].metadata_
    assert meta["written_by"] == agent_tools.AGENT_WRITER == "search_agent"
    assert s.added[0].source == "vera_memory"
    assert source_weight(s.added[0].source) <= 1.0


@pytest.mark.asyncio
async def test_loop_survives_garbage_tool_params_and_non_object_json():
    replies = [
        ("[1, 2]", {"provider": "x", "cost_usd": 0.0}),
        (json.dumps({"action": "tool", "name": "search_events",
                     "params": {"limit": 10**9, "q": 1}}), {"provider": "x"}),
        (json.dumps({"action": "tool", "name": "search_events", "params": "oops"}),
         {"provider": "x"}),
        (json.dumps({"action": "answer", "text": "ок"}), {"provider": "x"}),
    ]
    with patch.object(agent, "collect_tools",
                      AsyncMock(return_value=list(agent_tools.BUILTIN_SPECS))), \
         patch.object(agent, "chat_async", AsyncMock(side_effect=replies)):
        trace = await agent.run_agent(user_query="q", initial_context="",
                                      self_context="", history_block="", max_steps=5)
    assert trace.answer == "ок"
    assert trace.steps[0]["error"] == "not an object"
    assert trace.steps[1]["observation"]["error"] == "invalid params"
    assert trace.steps[2]["observation"]["error"] == "params must be a JSON object"


@pytest.mark.asyncio
async def test_out_of_steps_message_has_no_typo():
    with patch.object(agent, "collect_tools", AsyncMock(return_value=[])), \
         patch.object(agent, "chat_async",
                      AsyncMock(return_value=("{}", {"provider": "x"}))):
        trace = await agent.run_agent(user_query="q", initial_context="",
                                      self_context="", history_block="", max_steps=1)
    assert "уже́е" not in trace.answer and "уточнить" in trace.answer


@pytest.mark.asyncio
async def test_search_route_strips_project_words_and_passes_project(monkeypatch):
    from brain_search import app as search_app
    from brain_search.retrieval import Candidates

    monkeypatch.setenv("INTERNAL_SECRET", "s")
    fetch = AsyncMock(return_value=Candidates([], "project(veranda)+fts"))
    answer = AsyncMock(return_value="ANSWER")
    with patch.object(search_app, "_try_report", AsyncMock(return_value=None)), \
         patch.object(search_app, "embed_query", AsyncMock(return_value=None)), \
         patch.object(search_app, "fetch_candidates", fetch), \
         patch.object(search_app, "synthesize", answer):
        out = await search_app.search(
            SearchQuery(q="договор с поставщиком кальянов Веранда"), "s")
    assert out == "ANSWER"
    kwargs = fetch.await_args.kwargs
    assert kwargs["project"].name == "veranda"
    assert "Веранда" not in kwargs["ts_query"] and "договор:*" in kwargs["ts_query"]
    assert answer.await_args.kwargs["project"] == "veranda"


def test_untriaged_fresh_events_stay_visible_in_project_mode():
    from datetime import timedelta

    from brain_search.retrieval_filters import UNTRIAGED_GRACE
    from vera_shared.timeutil import utc_naive_now

    where, params = project_clause(SimpleNamespace(name="itstep"), None)
    assert "project IS NULL AND occurred_at > :fresh_after" in where
    age = utc_naive_now() - params["fresh_after"]
    assert abs(age - UNTRIAGED_GRACE) < timedelta(seconds=5)


@pytest.mark.parametrize(("q", "expected"), [
    ("edit step by step", None), ("что по IT Step", "itstep"),
    ("порядок в itstep", "itstep"), ("как дела в Веранде", "veranda"),
    ("my veranda", "veranda"), ("camelverand", None),
])
def test_project_triggers_match_at_word_start(q, expected):
    p = resolve_project(q)
    assert (p.name if p else None) == expected


def test_bot_flag_covers_metadata_and_username():
    from brain_search.rows import META_COLUMNS
    assert "LIKE '%bot'" in META_COLUMNS and "'is_bot'" in META_COLUMNS
