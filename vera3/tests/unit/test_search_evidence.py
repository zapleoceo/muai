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
from brain_search.evidence import EVIDENCE_RULES, evidence_excerpt, excerpt_terms
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
    mail = _mail("Introduction. " + "Additional details. " * 500 + cause)
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
    assert "2026-10-04 16:00 UTC" in seen["initial_context"]
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


def test_adjacent_login_alert_is_not_a_payment_cause_in_answer_prompts():
    context = (
        "[event:101 | 2026-10-04 02:00 | gmail] "
        "Date: October 3, 2026, 19:00 PDT. New login alert.\n\n"
        "[event:102 | 2026-10-04 16:00 | gmail] "
        "Advertising paused because the payment was declined. "
        "Account 00000000000000 is an advertising account identifier."
    )
    prompt = synthesis.build_prompt(
        question="Why were ads paused, and what is the number?",
        self_ctx="", context=context, history_block="", notes="",
    )
    assert context in prompt
    assert "Do not present a login or security alert as the cause" in prompt
    assert "without unsolicited speculative causes" in prompt
    assert "If asked for hypotheses, separate them" in prompt
    assert "label the time zone when dates differ" in prompt
    assert EVIDENCE_RULES in SYSTEM_PROMPT

    hypothesis_prompt = synthesis.build_prompt(
        question="What hypotheses could explain these two alerts?",
        self_ctx="", context=context, history_block="", notes="",
    )
    assert "label them unverified, and name the missing evidence" in hypothesis_prompt


@pytest.mark.asyncio
async def test_agent_search_tool_keeps_reason_at_end(monkeypatch):
    cause = "Payment declined, therefore ads paused."
    mail = _mail("x" * 9000 + cause)

    async def ranked(*args, **kwargs):
        return None, [(1.0, mail)]

    monkeypatch.setattr(agent_tools, "search_ranked", ranked)
    found = await agent_tools._exec_search_events(
        agent_tools.SearchEventsArgs(q="Why were ads paused?"))
    assert cause in found["events"][0]["preview"]
    assert found["events"][0]["occurred_at"].endswith(" UTC")
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


def test_search_observation_never_orphans_a_preview():
    events = [{
        "event_id": i, "source": "telegram", "preview": f"EVENT_{i} " + "x" * 640,
        "source_url": "https://example.invalid/" + "u" * 180,
        "author_label": "A" * 40, "chat_title": "C" * 100,
        "unneeded_metadata": "secret" * 1000,
    } for i in (101, 102, 103)]
    rendered = agent._observation_text("search_events", {"found": 3, "events": events})
    parsed = json.loads(rendered)
    assert len(rendered) <= 3000
    assert parsed["omitted_events"] == 3 - len(parsed["events"])
    assert "unneeded_metadata" not in rendered
    assert parsed["events"]
    for card in parsed["events"]:
        assert f"EVENT_{card['event_id']}" in card["preview"]
        assert card["source"] == "telegram"
    for omitted in events[len(parsed["events"]):]:
        assert f"EVENT_{omitted['event_id']}" not in rendered


def test_search_observation_keeps_three_compact_cards():
    events = [{"event_id": i, "source": "gmail", "preview": f"reason {i}",
               "chat_title": "T" * 10000, "source_url": "u" * 10000}
              for i in (201, 202, 203)]
    parsed = json.loads(agent._observation_text(
        "search_events", {"found": 3, "events": events}))
    assert [card["event_id"] for card in parsed["events"]] == [201, 202, 203]
    assert parsed["omitted_events"] == 0
    assert all(card["source_url"] is None for card in parsed["events"])
    assert all(len(card["chat_title"]) == 120 for card in parsed["events"])


WARNING = ("Найнебезпечніше — schools-purge-closed: пробний прогін через раннер "
           "неможливий, а бойове видалення незворотне. Також на стенді не задано "
           "SENTRY_RELEASE")
LAM_QUERIES = (
    ("LAM-274 event 516485 16 октября 2026: какие ограничения и риски остались "
    "после выкладки? Только это новое событие."),
    "Что с schools-purge-closed: dry-run возможен и задан ли SENTRY_RELEASE?",
)


def _filler_body(total: int, warning_at: int) -> str:
    filler = "Деплой завершён, миграции применены, проверки зелёные. "
    pad = (filler * (total // len(filler) + 1))
    return pad[:warning_at] + WARNING + pad[: total - warning_at - len(WARNING)]


@pytest.mark.parametrize("question", LAM_QUERIES)
def test_build_context_keeps_warning_in_middle_of_typical_email(question):
    body = _filler_body(4687, 2739)
    assert 4600 < len(body) < 4800
    result = SimpleNamespace(event_id=1, occurred_at="2026-10-16 09:00:00",
                             source="gmail", source_url=None)
    context = synthesis.build_context([result], {1: _mail(body)}, question)
    assert WARNING in context


def test_excerpt_with_terms_reveals_middle_of_huge_event():
    body = _filler_body(15000, 7000)
    excerpt = evidence_excerpt(body, 4000, ["SENTRY_RELEASE".lower()])
    assert len(excerpt) <= 4000
    assert "SENTRY_RELEASE" in excerpt
    assert "schools-purge-closed" in excerpt
    assert excerpt.count("middle of stored event omitted") == 2
    assert excerpt.startswith(body[:100]) and excerpt.endswith(body[-100:])
    assert "SENTRY_RELEASE" in excerpt_terms("Задан ли SENTRY_RELEASE?")


def test_excerpt_without_terms_or_match_is_head_tail_only():
    body = _filler_body(15000, 7000)
    plain = evidence_excerpt(body, 4000)
    assert evidence_excerpt(body, 4000, ["no-such-term"]) == plain
    assert evidence_excerpt(body, 4000, []) == plain
    assert WARNING not in plain
    assert len(plain) == 4000
    assert plain.count("middle of stored event omitted") == 1
