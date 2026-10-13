from __future__ import annotations

from brain_search.authorship import AUTHORSHIP_RULES
from brain_search.synthesis import build_prompt
from brain_triage.prompts import (
    _TRIAGE_RULES,
    TRIAGE_BATCH_PROMPT_HEADER,
    TRIAGE_PROMPT_TEMPLATE,
)

NEGATIVE_MARKERS = (
    "лише опис",
    "только описание",
    "no code changes",
    "description only",
    "не реалізовано",
    "не сделано",
    "решение за",
)


def test_authorship_rules_keep_negated_status_out_of_done():
    for marker in NEGATIVE_MARKERS:
        assert marker in AUTHORSHIP_RULES
    assert "не клади такое в «Что сделано»".lower() in AUTHORSHIP_RULES.lower()
    assert "Открыто / ждёт решения" in AUTHORSHIP_RULES
    assert "MR на staging или в ревью" in AUTHORSHIP_RULES
    assert "Done/Closed" in AUTHORSHIP_RULES


def test_synthesis_prompt_carries_negation_rule_in_stable_prefix():
    prompt = build_prompt(question="Q-UNIQUE", self_ctx="CFG", context="CTX",
                          history_block="", notes="")
    assert "Открыто / ждёт решения" in prompt
    assert prompt.index("Открыто / ждёт решения") < prompt.index("### Твоя конфигурация")
    assert prompt.index("Открыто / ждёт решения") < prompt.index("Q-UNIQUE")


def test_triage_rules_preserve_negation_in_both_prompts():
    assert "сохраняет отрицание и открытый статус" in _TRIAGE_RULES
    assert "никогда не пересказывай его как сделанное" in _TRIAGE_RULES
    assert _TRIAGE_RULES in TRIAGE_PROMPT_TEMPLATE
    assert _TRIAGE_RULES in TRIAGE_BATCH_PROMPT_HEADER
