"""rel_verify и проход verify чистки — с поддельным брокером."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from vera_shared.db.models import EventRow
from vera_shared.graph.rel_cleanup_verify import load_cache, verify_plan
from vera_shared.graph.rel_judge import judge_relationship
from vera_shared.graph.rel_text import End, Evidence
from vera_shared.graph.rel_validate import REJECT_WEAK_NAME
from vera_shared.graph.rel_verify import (
    ERROR,
    NO,
    UNCLEAR,
    YES,
    EdgeQuery,
    quote_in_text,
    verify_edge,
    verify_many,
)
from vera_shared.llm.client import LLMCoolingDown

TEXT = "Маша - моя дочь, ей уже пять лет"
Q = EdgeQuery(1, "Дима", "parent_of", "Маша")
BROKER = "vera_shared.graph.rel_verify.chat_async"


def reply(verdict, quote=""):
    return AsyncMock(return_value=(json.dumps({"verdict": verdict, "quote": quote}),
                                   {"cost_usd": 0.001}))


@pytest.mark.asyncio
async def test_yes_with_real_quote_is_accepted():
    with patch(BROKER, reply("yes", "Маша — моя ДОЧЬ")):
        got = await verify_edge(Q, TEXT)
    assert got.verdict == YES and got.cost_usd == pytest.approx(0.001)


@pytest.mark.asyncio
@pytest.mark.parametrize(("verdict", "expected"), [("no", NO), ("unclear", UNCLEAR)])
async def test_no_and_unclear_pass_through(verdict, expected):
    with patch(BROKER, reply(verdict)):
        assert (await verify_edge(Q, TEXT)).verdict == expected


@pytest.mark.asyncio
async def test_quote_not_in_text_means_no():
    with patch(BROKER, reply("yes", "она его дочь и наследница")):
        assert (await verify_edge(Q, TEXT)).verdict == NO
    with patch(BROKER, reply("yes", "")):
        assert (await verify_edge(Q, TEXT)).verdict == NO


@pytest.mark.asyncio
async def test_garbage_answer_and_outage_are_error_not_no():
    with patch(BROKER, AsyncMock(return_value=("not json", {}))):
        assert (await verify_edge(Q, TEXT)).verdict == ERROR
    with patch(BROKER, AsyncMock(side_effect=LLMCoolingDown("structured", 30))):
        assert (await verify_edge(Q, TEXT)).verdict == ERROR


@pytest.mark.asyncio
async def test_cache_hit_skips_broker_and_error_is_not_cached():
    cache = {}
    mock = reply("yes", "моя дочь")
    with patch(BROKER, mock):
        await verify_edge(Q, TEXT, cache=cache)
        await verify_edge(Q, TEXT, cache=cache)
    assert mock.await_count == 1
    failing = AsyncMock(side_effect=LLMCoolingDown("structured", 30))
    with patch(BROKER, failing):
        await verify_edge(EdgeQuery(2, "a", "friend_of", "b"), TEXT, cache=cache)
    assert len(cache) == 1


@pytest.mark.asyncio
async def test_concurrency_is_bounded():
    running = peak = 0

    async def slow(**_):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return json.dumps({"verdict": "no", "quote": ""}), {}

    items = [(EdgeQuery(i, "a", "friend_of", "b"), TEXT) for i in range(12)]
    with patch(BROKER, AsyncMock(side_effect=slow)):
        got = await verify_many(items, concurrency=3)
    assert len(got) == 12 and peak == 3


def test_quote_normalisation():
    assert quote_in_text("МАША — моя  дочь", TEXT)
    assert not quote_in_text("да", TEXT)


@pytest.mark.asyncio
async def test_judge_accepts_weak_name_only_on_verified_yes():
    ends = ((1, "Дима Петров", "person"), (2, "Маша", "person"))
    fact = "Дима Петров: Маша - моя дочь"
    ev = Evidence(fact, End(("Дима Петров",), strong=True), End(("Маша",)))
    with patch(BROKER, reply("yes", "моя дочь")):
        assert await judge_relationship(ends, "parent_of", 0.9, ev, event_id=1, body=TEXT) is None
    with patch(BROKER, reply("no")):
        assert await judge_relationship(
            ends, "parent_of", 0.9, ev, event_id=1, body=TEXT) == REJECT_WEAK_NAME
    with patch(BROKER, AsyncMock(side_effect=LLMCoolingDown("structured", 30))):
        assert await judge_relationship(
            ends, "parent_of", 0.9, ev, event_id=1, body=TEXT) == REJECT_WEAK_NAME


@pytest.mark.asyncio
async def test_judge_keeps_other_rules_after_yes():
    ends = ((1, "Дима Петров", "person"), (2, "Маша", "person"))
    ev = Evidence("ничего общего", End(("Дима Петров",), strong=True), End(("Маша",)))
    with patch(BROKER, reply("yes", "моя дочь")):
        assert await judge_relationship(
            ends, "parent_of", 0.9, ev, event_id=1, body=TEXT) == "fact_mismatch"


def _cand(rel_id, event_id):
    return {"id": rel_id, "subject_entity_id": 1, "predicate": "parent_of",
            "object_entity_id": 2, "is_current": True, "subject_name": "Дима",
            "object_name": "Маша", "derived_from_event_id": event_id, "fact": "f"}


@pytest.mark.asyncio
async def test_verify_plan_retires_no_keeps_yes_skips_outage_and_resumes(sqlite_db, tmp_path):
    now = datetime.now(UTC).replace(tzinfo=None)
    async with sqlite_db() as s:
        for i, body in enumerate((TEXT, "совсем другое", "третье"), 1):
            s.add(EventRow(id=i, source="telegram", source_event_id=f"e{i}",
                           content_text=f"Chat: x\n\n{body}", occurred_at=now,
                           received_at=now, triage_status="done"))
    cands = [_cand(1, 1), _cand(2, 2), _cand(3, 3)]
    cache = tmp_path / "plan.verdicts.jsonl"

    async def broker(**kw):
        if "третье" in kw["messages"][0]["content"]:
            raise LLMCoolingDown("structured", 30)
        yes = "моя дочь" in kw["messages"][0]["content"]
        return json.dumps({"verdict": "yes" if yes else "no",
                           "quote": "моя дочь" if yes else ""}), {"cost_usd": 0.002}

    with patch(BROKER, AsyncMock(side_effect=broker)):
        actions, stats = await verify_plan(cands, cache)
    rules = {a["rel_id"]: (a["action"], a["rule"]) for a in actions}
    assert rules == {1: ("skip", "weak_name_verified"), 2: ("retire", "weak_name")}
    assert (stats[YES], stats[NO], stats["unverified"]) == (1, 1, 1)
    assert len(load_cache(cache)) == 2

    again = AsyncMock(side_effect=broker)
    with patch(BROKER, again):
        _, stats2 = await verify_plan(cands, cache, limit=2)
    assert again.await_count == 0 and stats2["from_cache"] == 2
