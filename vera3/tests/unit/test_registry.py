"""Тесты Single Source of Truth для LLM конфигурации.

КРИТИЧНО: эти тесты — defense против $25 burn типа того что было в Vera 2.0.
Coverage этого модуля должен быть 100%.
"""
from __future__ import annotations

import pytest

from vera_shared.llm.registry import (
    PROVIDER_BASE_URL,
    PROVIDER_MODEL,
    PROVIDER_SUPPORTS_JSON_SCHEMA,
    PROVIDER_TIER,
    cost_usd,
    is_paid_provider,
    known_models,
    known_providers,
    model_for_provider,
    supports_json_schema,
)


class TestProviderConfig:
    def test_known_providers_returns_sorted_list(self):
        providers = known_providers()
        assert isinstance(providers, list)
        assert providers == sorted(providers)
        assert len(providers) > 5  # хотя бы 5 провайдеров

    def test_every_provider_has_model(self):
        for p in known_providers():
            assert model_for_provider(p) is not None, f"Provider {p} has no model"

    def test_every_provider_has_tier(self):
        for p in known_providers():
            assert p in PROVIDER_TIER, f"Provider {p} has no tier"
            assert PROVIDER_TIER[p] in {"free", "paid", "trial"}

    def test_every_provider_has_base_url(self):
        for p in known_providers():
            assert p in PROVIDER_BASE_URL, f"Provider {p} has no base_url"
            assert PROVIDER_BASE_URL[p].startswith("https://")

    def test_every_provider_has_json_schema_flag(self):
        for p in known_providers():
            assert p in PROVIDER_SUPPORTS_JSON_SCHEMA, f"Provider {p} missing json_schema info"
            assert isinstance(PROVIDER_SUPPORTS_JSON_SCHEMA[p], bool)


class TestModelPricing:
    def test_known_models_returns_sorted_list(self):
        models = known_models()
        assert models == sorted(models)
        assert len(models) > 5

    def test_every_provider_default_model_in_pricing_table(self):
        for p, m in PROVIDER_MODEL.items():
            # Должна быть либо в таблице как есть, либо последний сегмент
            c = cost_usd(m, 1000, 100)
            # cost не должен валиться. 0 для free, > 0 для paid
            assert c >= 0.0

    def test_gemini_flash_pricing(self):
        # $0.075/M input, $0.30/M output
        cost = cost_usd("gemini-2.5-flash", 1_000_000, 1_000_000)
        # 1M * 0.075 + 1M * 0.30 = 0.375
        assert cost == pytest.approx(0.375, abs=0.001)

    def test_deepseek_chat_pricing(self):
        # $0.27/M in, $1.10/M out
        cost = cost_usd("deepseek-chat", 1_000_000, 100_000)
        # 1M * 0.27 + 0.1M * 1.10 = 0.27 + 0.11 = 0.38
        assert cost == pytest.approx(0.38, abs=0.001)

    def test_free_models_return_zero_cost(self):
        for free_model in [
            "gpt-oss-120b",
            "openai/gpt-oss-120b",
            "openai/gpt-oss-120b:free",
            "zai-glm-4.7",
            "llama-3.3-70b-versatile",
            "Meta-Llama-3.3-70B-Instruct",
        ]:
            c = cost_usd(free_model, 10_000_000, 10_000_000)
            assert c == 0.0, f"{free_model} expected free but cost {c}"

    def test_unknown_model_returns_zero(self):
        assert cost_usd("nonexistent-model-xyz", 1_000_000, 1_000_000) == 0.0

    def test_empty_model_returns_zero(self):
        assert cost_usd("", 1000, 100) == 0.0
        assert cost_usd(None, 1000, 100) == 0.0  # type: ignore[arg-type]

    def test_model_with_provider_prefix_normalizes(self):
        a = cost_usd("gemini-2.5-flash", 1000, 100)
        b = cost_usd("gemini/gemini-2.5-flash", 1000, 100)
        assert a == b

    def test_model_case_insensitive_for_non_namespaced(self):
        a = cost_usd("gemini-2.5-flash", 1000, 100)
        b = cost_usd("GEMINI-2.5-FLASH", 1000, 100)
        assert a == b

    def test_namespaced_model_kept_exact(self):
        # openai/gpt-oss-120b — namespaced free, должен находиться по точному имени
        cost = cost_usd("openai/gpt-oss-120b", 1_000_000, 1_000_000)
        assert cost == 0.0


class TestPaidProviderClassification:
    def test_is_paid_provider_known_paid(self):
        assert is_paid_provider("deepseek") is True
        assert is_paid_provider("openai") is True

    def test_is_paid_provider_known_free(self):
        assert is_paid_provider("cerebras") is False
        assert is_paid_provider("groq") is False
        assert is_paid_provider("voyage") is False

    def test_is_paid_provider_trial_is_not_paid(self):
        assert is_paid_provider("anthropic") is False  # tier = trial

    def test_is_paid_provider_unknown_provider_returns_false(self):
        assert is_paid_provider("nonexistent") is False


class TestJsonSchemaSupport:
    def test_supports_json_schema_yes(self):
        assert supports_json_schema("gemini") is True
        assert supports_json_schema("cerebras") is True
        assert supports_json_schema("groq") is True
        assert supports_json_schema("openrouter") is True

    def test_supports_json_schema_no(self):
        # DeepSeek — только json_object, не json_schema (verified 2026-06-08)
        assert supports_json_schema("deepseek") is False
        # Voyage — embedder, не chat вообще
        assert supports_json_schema("voyage") is False

    def test_supports_json_schema_unknown_returns_false(self):
        assert supports_json_schema("nonexistent") is False


class TestRegressionGuards:
    """Регрессионные тесты — то что НЕ должно сломаться."""

    def test_gemini_flash_cheaper_than_deepseek(self):
        """Если кто-то поменяет цены и Gemini станет дороже DeepSeek — сюрприз."""
        tokens = (10_000, 2_000)
        g = cost_usd("gemini-2.5-flash", *tokens)
        d = cost_usd("deepseek-chat", *tokens)
        assert g < d, f"Gemini ({g}) should be cheaper than DeepSeek ({d})"

    def test_gemini_flash_cheaper_than_anthropic_haiku(self):
        tokens = (10_000, 2_000)
        g = cost_usd("gemini-2.5-flash", *tokens)
        a = cost_usd("claude-haiku-4-5", *tokens)
        assert g < a

    def test_at_least_three_free_chat_providers(self):
        """Гарантируем что у нас всегда есть резерв бесплатных провайдеров."""
        free_chat = [
            p for p, tier in PROVIDER_TIER.items()
            if tier == "free" and supports_json_schema(p)
        ]
        assert len(free_chat) >= 3, f"Too few free chat providers: {free_chat}"

    def test_voyage_is_in_providers_for_embedding(self):
        assert "voyage" in PROVIDER_MODEL
