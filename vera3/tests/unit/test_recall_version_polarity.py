"""Room #3920: a "fixed in" version must not be recalled as "affected"."""
from __future__ import annotations

import os
import sys
from datetime import UTC, datetime

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(__file__), "..", "..", "services", "brain-search", "src"))

from brain_search import synthesis
from brain_search.agent import SYSTEM_PROMPT
from brain_search.evidence import POLARITY_RULES, evidence_excerpt, excerpt_terms
from brain_search.models import SearchQuery
from brain_search.rows import Candidate

FIX_SENTENCE = (
    "PHP 8.5.12, 8.4.27, 8.3.36 and 8.2.35 fix two critical vulnerabilities "
    "that can allow unauthenticated remote code execution.")


def _candidate(event_id: int) -> Candidate:
    return Candidate(
        id=event_id, source="gmail", source_event_id=f"php-{event_id}",
        occurred_at=datetime(2026, 10, 20, 9, 0, tzinfo=UTC),
        content_text=f"Subject: PHP security release\n---\n{FIX_SENTENCE}",
        importance=70, embedding=None, rank=1.0, account="test@example.com",
    )


def test_polarity_rules_precede_config_and_question_in_synthesis_prompt():
    prompt = synthesis.build_prompt(question="Q-UNIQUE", self_ctx="CFG",
                                    context="CTX", history_block="", notes="")
    assert POLARITY_RULES in prompt
    assert prompt.index(POLARITY_RULES) < prompt.index("### Твоя конфигурация")
    assert prompt.index(POLARITY_RULES) < prompt.index("Q-UNIQUE")
    assert prompt.index(POLARITY_RULES) < prompt.index("### Найденные события")


def test_polarity_rules_in_agent_system_prompt():
    assert POLARITY_RULES in SYSTEM_PROMPT
    assert "NOT affected" in POLARITY_RULES
    assert "quote the exact source phrase" in POLARITY_RULES


def test_polarity_rules_bind_polarity_to_the_subject_in_both_directions():
    assert "Version X fixes/addresses" in POLARITY_RULES
    assert "X contains the fix and is NOT affected" in POLARITY_RULES
    assert "a flaw/vulnerability in X" in POLARITY_RULES
    assert "X is vulnerable/affected\" mean X is affected" in POLARITY_RULES


@pytest.mark.asyncio
async def test_synthesis_sends_full_fix_sentence_with_polarity_rules(monkeypatch):
    sent = {}

    async def self_context():
        return ""

    async def chat(*, messages, **kwargs):
        sent["prompt"] = messages[0]["content"]
        return "ok", {"provider": "test", "cost_usd": 0.0}

    monkeypatch.setattr(synthesis, "self_context", self_context)
    monkeypatch.setattr(synthesis, "chat_async", chat)
    result = await synthesis.answer(
        SearchQuery(q="Which PHP versions are affected?", limit=2,
                    use_agent=False),
        [_candidate(524727), _candidate(525042)], None,
    )

    assert result.answer == "ok"
    prompt = sent["prompt"]
    assert prompt.count("fix two critical vulnerabilities") == 2
    assert "event:524727" in prompt and "event:525042" in prompt
    assert POLARITY_RULES in prompt


def _long_record(middle: str) -> str:
    text = ("Intro. " + "filler text. " * 700 + middle + " "
            + "more filler. " * 700 + "Regards.")
    assert len(text) > 8000
    return text


def test_excerpt_keeps_fix_sentence_when_version_is_a_term():
    """Regression guard for #59 window logic; passes on master too."""
    excerpt = evidence_excerpt(_long_record(FIX_SENTENCE), terms=["8.4.27"])
    assert "fix two critical vulnerabilities" in excerpt


def test_excerpt_keeps_fix_sentence_found_by_cve_id_from_the_question():
    """Regression guard for #59 window logic; passes on master too."""
    middle = f"CVE-2026-109618 and CVE-2026-109619: {FIX_SENTENCE}"
    terms = excerpt_terms("What is CVE-2026-109618?")
    assert "109618" in terms
    excerpt = evidence_excerpt(_long_record(middle), terms=terms)
    assert "fix two critical vulnerabilities" in excerpt
