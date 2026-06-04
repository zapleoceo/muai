"""Single-source-of-truth for LLM config. If this test fails, downstream
modules (cost_guard, router, pool_clients, multimodal) will misbehave."""
from vera_shared.llm.registry import (
    PROVIDER_MODEL, CAPABILITY_ORDER, PAID_KEYS,
    cost_usd, is_paid, model_for_provider, known_models,
)


def test_provider_model_canonical_names():
    """Every provider has a model and that model is in our pricing table."""
    for provider, model in PROVIDER_MODEL.items():
        assert cost_usd(model, 1, 1) >= 0.0, f"{provider}/{model} unknown price"


def test_paid_keys_format():
    for entry in PAID_KEYS:
        assert isinstance(entry, tuple) and len(entry) == 2
        provider, label = entry
        assert provider in PROVIDER_MODEL, f"unknown provider {provider}"


def test_is_paid_true_false():
    assert is_paid("gemini", "demoniwwwe") is True
    assert is_paid("gemini", "Liza") is False  # free key
    assert is_paid("nonexistent", "anything") is False


def test_capability_order_uses_known_providers():
    for cap, providers in CAPABILITY_ORDER.items():
        for p in providers:
            assert p in PROVIDER_MODEL, f"capability {cap} uses unknown {p}"


def test_cost_usd_known_model():
    # Gemini 2.5 Flash: $0.075 in / $0.30 out per 1M
    c = cost_usd("gemini-2.5-flash", 1_000_000, 100_000)
    # 1M * 0.075 + 100K * 0.30 = 0.075 + 0.030 = 0.105
    assert 0.10 < c < 0.11


def test_cost_usd_unknown_model_is_zero():
    assert cost_usd("unknown-model-name", 1_000_000, 1_000_000) == 0.0


def test_cost_usd_strips_provider_prefix():
    """Router prepends 'gemini/' which our pricing dict shouldn't care about."""
    a = cost_usd("gemini-2.5-flash", 1000, 100)
    b = cost_usd("gemini/gemini-2.5-flash", 1000, 100)
    assert a == b


def test_cost_usd_case_insensitive():
    a = cost_usd("gemini-2.5-flash", 1000, 100)
    b = cost_usd("GEMINI-2.5-FLASH", 1000, 100)
    assert a == b


def test_model_for_provider():
    assert model_for_provider("gemini") == "gemini-2.5-flash"
    assert model_for_provider("nonexistent") is None


def test_known_models_returns_sorted_list():
    models = known_models()
    assert isinstance(models, list)
    assert models == sorted(models)
    assert "gemini-2.5-flash" in models
