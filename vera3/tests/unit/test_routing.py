"""Тесты routing policy — гарантия что free всегда впереди paid."""
from __future__ import annotations

import pytest

from vera_shared.llm.routing import Capability, RoutingPolicy, ProviderChoice


class TestChainBuilding:
    def test_chat_fast_includes_known_free_providers(self):
        chain = RoutingPolicy.chain_for("chat:fast")
        providers = [c.provider for c in chain]
        assert "cerebras" in providers
        assert "groq" in providers
        assert "gemini" in providers

    def test_chat_fast_first_is_free(self):
        chain = RoutingPolicy.chain_for("chat:fast")
        assert chain[0].is_free, f"First provider for chat:fast must be free, got {chain[0]}"

    def test_paid_providers_at_the_end(self):
        chain = RoutingPolicy.chain_for("chat:fast")
        # Все paid идут после всех free
        paid_indices = [i for i, c in enumerate(chain) if c.is_paid]
        free_indices = [i for i, c in enumerate(chain) if c.is_free]
        if paid_indices and free_indices:
            assert max(free_indices) < min(paid_indices), \
                "Free providers must precede all paid providers"

    def test_include_paid_false_filters(self):
        chain = RoutingPolicy.chain_for("chat:fast", include_paid=False)
        assert all(not c.is_paid for c in chain)

    def test_require_json_schema_filters_out_unsupported(self):
        # DeepSeek не поддерживает json_schema
        chain = RoutingPolicy.chain_for("structured", require_json_schema=True)
        providers = [c.provider for c in chain]
        assert "deepseek" not in providers, "DeepSeek does not support json_schema"

    def test_invalid_capability_raises(self):
        with pytest.raises(ValueError):
            RoutingPolicy.chain_for("not-a-capability")  # type: ignore[arg-type]


class TestFreeFirstInvariant:
    """Главный инвариант: free всегда раньше paid в любой capability."""

    @pytest.mark.parametrize("cap", ["chat:fast", "chat:smart", "chat:code", "prefilter", "structured"])
    def test_free_first_for_each_capability(self, cap: Capability):
        # verify_free_first сам поднимет AssertionError если нарушится
        RoutingPolicy.verify_free_first(cap)


class TestProviderChoice:
    def test_is_paid_property(self):
        c = ProviderChoice(provider="openai", tier="paid")
        assert c.is_paid is True
        assert c.is_free is False

    def test_is_free_property(self):
        c = ProviderChoice(provider="cerebras", tier="free")
        assert c.is_free is True
        assert c.is_paid is False

    def test_trial_is_neither_paid_nor_free(self):
        c = ProviderChoice(provider="anthropic", tier="trial")
        assert c.is_paid is False
        assert c.is_free is False


class TestStructuredCapability:
    """Specifically для Graphiti use case — нужен json_schema."""

    def test_structured_chain_only_includes_json_schema_capable(self):
        chain = RoutingPolicy.chain_for("structured", require_json_schema=True)
        from vera_shared.llm.registry import supports_json_schema
        for c in chain:
            assert supports_json_schema(c.provider), \
                f"{c.provider} in structured chain but doesn't support json_schema"

    def test_structured_chain_has_free_options(self):
        chain = RoutingPolicy.chain_for("structured", require_json_schema=True)
        free = [c for c in chain if c.is_free]
        assert len(free) >= 3, "Need at least 3 free structured providers"


class TestPrefilterCapability:
    """Prefilter — самые быстрые и легкие, не для structured."""

    def test_prefilter_has_only_free(self):
        chain = RoutingPolicy.chain_for("prefilter")
        # Prefilter не должен использовать paid (это лёгкая операция)
        paid = [c for c in chain if c.is_paid]
        assert len(paid) == 0
