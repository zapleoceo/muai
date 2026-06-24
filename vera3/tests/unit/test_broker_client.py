"""vera_shared.llm.broker_client — toggling, response unpacking, fallback."""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import vera_shared.llm.broker_client as bc


def test_broker_enabled_requires_both_vars(monkeypatch):
    monkeypatch.setattr(bc, "BROKER_URL", "")
    monkeypatch.setattr(bc, "BROKER_PROJECT_KEY", "")
    assert not bc.broker_enabled()

    monkeypatch.setattr(bc, "BROKER_URL", "https://aib.zapleo.com")
    monkeypatch.setattr(bc, "BROKER_PROJECT_KEY", "")
    assert not bc.broker_enabled()

    monkeypatch.setattr(bc, "BROKER_URL", "")
    monkeypatch.setattr(bc, "BROKER_PROJECT_KEY", "aib_prj_xxx")
    assert not bc.broker_enabled()

    monkeypatch.setattr(bc, "BROKER_URL", "https://aib.zapleo.com")
    monkeypatch.setattr(bc, "BROKER_PROJECT_KEY", "aib_prj_xxx")
    assert bc.broker_enabled()


@pytest.mark.asyncio
async def test_chat_via_broker_unpacks_response(monkeypatch):
    monkeypatch.setattr(bc, "BROKER_URL", "https://aib.zapleo.com")
    monkeypatch.setattr(bc, "BROKER_PROJECT_KEY", "aib_prj_xxx")
    monkeypatch.setattr(bc, "_http", None)

    fake = AsyncMock()
    fake.status_code = 200
    fake.json = lambda: {
        "text": "hello dima",
        "provider": "cerebras",
        "model": "cerebras/gpt-oss-120b",
        "tokens_in": 12,
        "tokens_out": 3,
        "cost_usd": 0.0,
        "latency_ms": 451,
    }

    with patch.object(httpx.AsyncClient, "post", AsyncMock(return_value=fake)):
        with patch.object(bc, "_log_usage", AsyncMock()):
            text, meta = await bc.chat_via_broker(
                messages=[{"role": "user", "content": "x"}],
                capability="chat:fast",
            )
    assert text == "hello dima"
    assert meta["provider"] == "cerebras"
    assert meta["tokens_in"] == 12
    assert meta["latency_ms"] == 451


@pytest.mark.asyncio
async def test_chat_via_broker_raises_on_5xx(monkeypatch):
    monkeypatch.setattr(bc, "BROKER_URL", "https://aib.zapleo.com")
    monkeypatch.setattr(bc, "BROKER_PROJECT_KEY", "aib_prj_xxx")
    monkeypatch.setattr(bc, "_http", None)

    fake = AsyncMock()
    fake.status_code = 503
    fake.text = "all providers exhausted"
    with patch.object(httpx.AsyncClient, "post", AsyncMock(return_value=fake)):
        with pytest.raises(bc.BrokerCallFailed, match="503"):
            await bc.chat_via_broker(
                messages=[{"role": "user", "content": "x"}],
                capability="chat:fast",
            )


@pytest.mark.asyncio
async def test_embed_via_broker_with_str_input(monkeypatch):
    """str input wraps to [input] — verify it doesn't iterate over chars."""
    monkeypatch.setattr(bc, "BROKER_URL", "https://aib.zapleo.com")
    monkeypatch.setattr(bc, "BROKER_PROJECT_KEY", "aib_prj_xxx")
    monkeypatch.setattr(bc, "_http", None)

    captured = {}

    async def fake_post(self, url, params=None, json=None):
        captured["url"] = url
        captured["json"] = json
        r = AsyncMock()
        r.status_code = 200
        r.json = lambda: {
            "embeddings": [[0.1, 0.2, 0.3]],
            "provider": "voyage",
            "model": "voyage/voyage-3",
            "tokens_in": 1,
            "cost_usd": 0.0,
            "latency_ms": 99,
        }
        return r

    with patch.object(httpx.AsyncClient, "post", fake_post):
        with patch.object(bc, "_log_usage", AsyncMock()):
            vectors = await bc.embed_via_broker("hello")

    assert vectors == [[0.1, 0.2, 0.3]]
    assert captured["json"]["input"] == ["hello"]  # NOT ['h', 'e', 'l', ...]
