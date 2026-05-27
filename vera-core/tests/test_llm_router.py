"""Tests for vera_shared.llm.router — without making real LLM calls."""
import pytest

from vera_shared.llm import router as r


@pytest.mark.asyncio
async def test_build_model_list_picks_active_tokens(sample_tokens):
    await r.reset_router()
    model_list = await r._build_model_list()
    providers = sorted({m["model_info"]["provider"] for m in model_list})
    assert "gemini" in providers
    assert "deepseek" in providers


@pytest.mark.asyncio
async def test_capability_order_has_known_aliases():
    assert "chat:fast" in r._CAPABILITY_ORDER
    assert "chat:smart" in r._CAPABILITY_ORDER
    # fast tries free Gemini pool first; smart leans on openrouter+deepseek
    # before touching anthropic. Paid keys are deprioritised via weight.
    assert r._CAPABILITY_ORDER["chat:fast"][0] == "gemini"
    assert "openrouter" in r._CAPABILITY_ORDER["chat:fast"]


def test_openrouter_registered_with_openai_compat():
    info = r._PROVIDER_MODEL.get("openrouter")
    assert info is not None
    provider_prefix, model = info
    assert provider_prefix == "openrouter"
    assert ":free" in model  # we want a free-tier model on OpenRouter


def test_paid_label_known():
    # demoniwwwe is the only paid Gemini key right now.
    assert "demoniwwwe" in r._PAID_LABELS


def test_gemini_uses_2_5_flash():
    """2.0-flash deprecated 2026-06-01. Must use 2.5 or newer."""
    info = r._PROVIDER_MODEL["gemini"]
    _, model = info
    assert "2.5" in model or "3" in model, f"got {model}, must be ≥2.5"


@pytest.mark.asyncio
async def test_get_router_without_tokens_raises(monkeypatch):
    """If DB has zero active tokens, router should fail loud, not silently."""
    await r.reset_router()
    async def _empty():
        return []
    monkeypatch.setattr(r, "_build_model_list", _empty)
    with pytest.raises(RuntimeError):
        await r._get_router()
