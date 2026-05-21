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
    # Smart should try Anthropic first, fast tries Gemini first
    assert r._CAPABILITY_ORDER["chat:smart"][0] == "anthropic"
    assert r._CAPABILITY_ORDER["chat:fast"][0] == "gemini"


@pytest.mark.asyncio
async def test_get_router_without_tokens_raises(monkeypatch):
    """If DB has zero active tokens, router should fail loud, not silently."""
    await r.reset_router()
    async def _empty():
        return []
    monkeypatch.setattr(r, "_build_model_list", _empty)
    with pytest.raises(RuntimeError):
        await r._get_router()
