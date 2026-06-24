"""Тесты postprocess_triage — валидация nature/project классификации."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(__file__), "..", "..",
    "services", "brain-triage", "src",
))

from brain_triage.worker import (  # noqa: E402
    NATURE_BY_SOURCE,
    PROJECT_VOCAB,
    SKIP_EMBED_SOURCES,
    VALID_NATURES,
    postprocess_triage,
)


class TestNature:
    def test_vera_chat_forced_conversation(self):
        # LLM может сказать что угодно — source перекрывает
        parsed = postprocess_triage({"nature": "world_event"}, "vera_chat")
        assert parsed["nature"] == "conversation_with_me"

    def test_perplexity_forced_intent(self):
        parsed = postprocess_triage({"nature": "world_event"}, "perplexity")
        assert parsed["nature"] == "my_intent"

    def test_vera_memory_forced_derived(self):
        parsed = postprocess_triage({}, "vera_memory")
        assert parsed["nature"] == "derived_fact"

    def test_gmail_uses_llm_field(self):
        parsed = postprocess_triage({"nature": "my_intent"}, "gmail")
        assert parsed["nature"] == "my_intent"  # черновик письма — валидно

    def test_unknown_source_invalid_nature_defaults_world(self):
        parsed = postprocess_triage({"nature": "galaxy_event"}, "whatsapp")
        assert parsed["nature"] == "world_event"

    def test_missing_nature_defaults_world(self):
        parsed = postprocess_triage({}, "telegram")
        assert parsed["nature"] == "world_event"


class TestProject:
    def test_valid_project_kept(self):
        parsed = postprocess_triage({"project": "itstep"}, "telegram")
        assert parsed["project"] == "itstep"

    def test_case_normalized(self):
        parsed = postprocess_triage({"project": "ITSTEP "}, "gmail")
        assert parsed["project"] == "itstep"

    def test_hallucinated_project_falls_to_other(self):
        # LLM выдумал категорию вне словаря — не даём загрязнить колонку
        parsed = postprocess_triage({"project": "jakarta-academy"}, "gmail")
        assert parsed["project"] == "other"

    def test_missing_project_falls_to_other(self):
        parsed = postprocess_triage({}, "telegram")
        assert parsed["project"] == "other"

    def test_vocab_is_closed_set(self):
        assert PROJECT_VOCAB == {"itstep", "veranda", "family",
                                 "personal", "news", "other"}


class TestEmbedSkip:
    def test_intent_sources_not_embedded(self):
        # Вектора запросов к AI засоряют семантический поиск
        assert "perplexity" in SKIP_EMBED_SOURCES
        assert "vera_chat" in SKIP_EMBED_SOURCES

    def test_world_sources_embedded(self):
        assert "gmail" not in SKIP_EMBED_SOURCES
        assert "telegram" not in SKIP_EMBED_SOURCES


class TestVocabConsistency:
    def test_nature_by_source_values_valid(self):
        assert set(NATURE_BY_SOURCE.values()) <= VALID_NATURES

    def test_preserves_other_fields(self):
        parsed = postprocess_triage(
            {"importance": 70, "topics": ["финансы"], "project": "veranda"},
            "telegram",
        )
        assert parsed["importance"] == 70
        assert parsed["topics"] == ["финансы"]


class TestReadySubtype:
    def test_ready_deal_preserved(self):
        parsed = postprocess_triage(
            {
                "needs_action": True,
                "ready_subtype": "deal",
                "nature": "world_event",
                "project": "itstep",
            },
            "telegram",
        )
        assert parsed["ready_subtype"] == "deal"

    def test_ready_openhouse_preserved(self):
        parsed = postprocess_triage(
            {
                "needs_action": True,
                "ready_subtype": "openhouse",
                "nature": "world_event",
                "project": "itstep",
            },
            "telegram",
        )
        assert parsed["ready_subtype"] == "openhouse"

    def test_ready_subtype_normalized_to_lowercase(self):
        parsed = postprocess_triage(
            {
                "needs_action": True,
                "ready_subtype": "DEAL",  # uppercase
                "nature": "world_event",
                "project": "itstep",
            },
            "telegram",
        )
        assert parsed["ready_subtype"] == "deal"

    def test_ready_subtype_with_whitespace_normalized(self):
        parsed = postprocess_triage(
            {
                "needs_action": True,
                "ready_subtype": "  openhouse  ",
                "nature": "world_event",
                "project": "itstep",
            },
            "telegram",
        )
        assert parsed["ready_subtype"] == "openhouse"

    def test_ready_subtype_cleared_if_not_needs_action(self):
        parsed = postprocess_triage(
            {
                "needs_action": False,
                "ready_subtype": "deal",  # should be cleared
                "nature": "world_event",
                "project": "itstep",
            },
            "telegram",
        )
        assert parsed["ready_subtype"] is None

    def test_ready_subtype_invalid_becomes_null(self):
        parsed = postprocess_triage(
            {
                "needs_action": True,
                "ready_subtype": "invalid_type",
                "nature": "world_event",
                "project": "itstep",
            },
            "telegram",
        )
        assert parsed["ready_subtype"] is None

    def test_ready_subtype_null_if_missing(self):
        parsed = postprocess_triage(
            {
                "needs_action": True,
                "nature": "world_event",
                "project": "itstep",
                # no ready_subtype key
            },
            "telegram",
        )
        assert parsed["ready_subtype"] is None

    def test_ready_subtype_null_when_needs_action_false(self):
        parsed = postprocess_triage(
            {
                "needs_action": False,
                # no ready_subtype key
                "nature": "world_event",
                "project": "itstep",
            },
            "telegram",
        )
        assert parsed["ready_subtype"] is None
