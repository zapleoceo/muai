"""Answer context must preserve the evidence needed for causal claims."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(__file__), "..", "..", "services", "brain-search", "src"))

from brain_search import agent, agent_tools, synthesis
from brain_search.agent import SYSTEM_PROMPT
from brain_search.evidence import EVIDENCE_RULES, evidence_excerpt
from brain_search.models import SearchQuery
from brain_search.rows import Candidate


def _mail(body: str) -> Candidate:
    return Candidate(
        id=1, source="gmail", source_event_id="synthetic-mail",
        occurred_at=datetime(2026, 10, 4, 16, 0, tzinfo=timezone.utc),
        content_text=(
            "Subject: Advertising access temporarily unavailable\n"
            "---\n" + body),
        importance=70, embedding=None, rank=1.0, account="test@example.com",
    )


@pytest.mark.asyncio
async def test_answer_context_keeps_payment_cause_at_end(monkeypatch):
    cause = ("Payment declined, so ads were paused. 12345678901234 is the "
             "advertising account identifier, not a transaction ID.")
    mail = _mail("Introduction. " + "Additional details. " * 300 + cause)
    seen = {}

    async def self_context():
        return ""

    async def run_agent(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(answer="ok", provider_last="test", cost_usd=0,
                               final_step=1, steps=[])

    monkeypatch.setattr(synthesis, "self_context", self_context)
    monkeypatch.setattr(synthesis, "run_agent", run_agent)
    result = await synthesis.answer(
        SearchQuery(q="Why were ads paused?", limit=1),
        [mail], None,
    )

    assert result.answer == "ok"
    assert cause in seen["initial_context"]
    assert "middle of stored event omitted" in seen["initial_context"]
    assert cause not in result.results[0].content_preview


def test_truncated_excerpts_do_not_license_absence_or_reversed_causality():
    excerpt = evidence_excerpt("header " + "x" * 4500 + " cause at end", 900)
    assert "cause at end" in excerpt
    assert "middle of stored event omitted" in excerpt
    prompt = synthesis.build_prompt(question="why", self_ctx="", context=excerpt,
                                    history_block="", notes="")
    assert EVIDENCE_RULES in prompt
    assert EVIDENCE_RULES in SYSTEM_PROMPT
    assert "account identifiers from payment" in EVIDENCE_RULES


@pytest.mark.asyncio
async def test_agent_search_tool_keeps_reason_at_end(monkeypatch):
    cause = "Payment declined, therefore ads paused."
    mail = _mail("x" * 5000 + cause)

    async def ranked(*args, **kwargs):
        return None, [(1.0, mail)]

    monkeypatch.setattr(agent_tools, "search_ranked", ranked)
    found = await agent_tools._exec_search_events(
        agent_tools.SearchEventsArgs(q="Why were ads paused?"))
    assert cause in found["events"][0]["preview"]
    assert "middle of stored event omitted" in found["events"][0]["preview"]


@pytest.mark.asyncio
async def test_agent_next_step_receives_tail_of_search_observation(monkeypatch):
    cause = "Payment declined, so ads were paused."
    long_preview = evidence_excerpt("header " + "x" * 5000 + cause, 4000)
    observation = {"found": 1, "events": [{
        "event_id": 1, "source": "gmail", "preview": long_preview,
    }]}
    replies = [
        (json.dumps({"action": "tool", "name": "search_events", "params": {"q": "ads"}}), {}),
        (json.dumps({"action": "answer", "text": "ok"}), {}),
    ]
    chat = AsyncMock(side_effect=replies)
    monkeypatch.setattr(agent, "collect_tools", AsyncMock(return_value=[
        agent.ToolDescriptor("search_events", "Search", {}, "builtin:search_events")
    ]))
    monkeypatch.setattr(agent, "execute_tool", AsyncMock(return_value=observation))
    monkeypatch.setattr(agent, "chat_async", chat)

    result = await agent.run_agent(user_query="Why?", initial_context="",
                                   self_context="", history_block="")
    next_prompt = chat.call_args_list[1].kwargs["messages"][0]["content"]
    assert result.answer == "ok"
    assert cause in next_prompt
    assert "middle of stored event omitted" in next_prompt
