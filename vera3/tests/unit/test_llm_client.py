"""vera_shared.llm.client — thin broker wrapper, error translation."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import vera_shared.llm.client as client


@pytest.mark.asyncio
async def test_chat_async_delegates_to_broker(monkeypatch):
    monkeypatch.setattr(client, "broker_enabled", lambda: True)
    with patch.object(client, "chat_async_via_broker",
                       AsyncMock(return_value=("hi", {"provider": "cerebras"}))) as m:
        text, meta = await client.chat_async(
            [{"role": "user", "content": "x"}], capability="chat:fast", workflow="test",
        )
    assert text == "hi"
    assert meta["provider"] == "cerebras"
    m.assert_awaited_once()


@pytest.mark.asyncio
async def test_chat_async_raises_llmcallfailed_on_broker_failure(monkeypatch):
    monkeypatch.setattr(client, "broker_enabled", lambda: True)
    with patch.object(client, "chat_async_via_broker",
                       AsyncMock(side_effect=client.BrokerCallFailed("boom"))):
        with pytest.raises(client.LLMCallFailed, match="boom"):
            await client.chat_async([{"role": "user", "content": "x"}])


@pytest.mark.asyncio
async def test_chat_async_requires_broker_enabled(monkeypatch):
    monkeypatch.setattr(client, "broker_enabled", lambda: False)
    with pytest.raises(client.LLMCallFailed, match="BROKER_URL"):
        await client.chat_async([{"role": "user", "content": "x"}])
