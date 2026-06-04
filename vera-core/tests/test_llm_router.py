"""Tests for vera_shared.llm.router — without making real LLM calls."""
import pytest

from vera_shared.llm import router as r
from vera_shared.llm import registry as reg


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
    # Cerebras + Groq go first for chat/triage — their daily quota is
    # orders of magnitude bigger than Gemini's, and they leave Gemini's
    # tiny RPM window available for Graphiti (the only consumer that
    # CAN'T switch off Gemini due to its structured-output requirement).
    assert r._CAPABILITY_ORDER["chat:fast"][0] == "cerebras"
    assert "groq" in r._CAPABILITY_ORDER["chat:fast"]
    assert "gemini" in r._CAPABILITY_ORDER["chat:fast"]
    assert "openrouter" in r._CAPABILITY_ORDER["chat:fast"]


def test_openrouter_registered_with_openai_compat():
    model = reg.PROVIDER_MODEL.get("openrouter")
    assert model is not None
    assert ":free" in model  # we want a free-tier model on OpenRouter


def test_paid_keys_provider_scoped():
    # ONLY gemini-demoniwwwe is paid. DeepSeek/Voyage even though same
    # label «demoniwwwe» are free tier — must not be flagged paid.
    assert reg.is_paid("gemini", "demoniwwwe") is True
    assert reg.is_paid("deepseek", "demoniwwwe") is False


def test_gemini_uses_2_5_flash():
    """2.0-flash deprecated 2026-06-01. Must use 2.5 or newer."""
    model = reg.PROVIDER_MODEL["gemini"]
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
