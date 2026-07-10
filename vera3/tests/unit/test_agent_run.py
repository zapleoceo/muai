"""brain_search.agent.run_agent — ReAct loop: answer/timeout/error/malformed-JSON
branches. collect_tools() and chat_async are mocked at the boundary — real
tool execution and broker calls are exercised elsewhere (integration)."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
import brain_search.agent as agent


@pytest.mark.asyncio
async def test_run_agent_answers_on_first_step():
    step_reply = json.dumps({"action": "answer", "text": "готово"})

    with patch.object(agent, "collect_tools", AsyncMock(return_value=[])), \
         patch.object(agent, "chat_async",
                       AsyncMock(return_value=(step_reply, {"provider": "cerebras",
                                                             "cost_usd": 0.001}))):
        trace = await agent.run_agent(
            user_query="q", initial_context="", self_context="", history_block="",
        )

    assert trace.answer == "готово"
    assert trace.final_step == 1
    assert trace.provider_last == "cerebras"
    assert trace.cost_usd == pytest.approx(0.001)


@pytest.mark.asyncio
async def test_run_agent_timeout_sets_message(monkeypatch):
    monkeypatch.setattr(agent, "AGENT_STEP_TIMEOUT_S", 0.05)

    async def _slow_chat(**kwargs):
        import asyncio
        await asyncio.sleep(1)
        return "unreachable", {}

    with patch.object(agent, "collect_tools", AsyncMock(return_value=[])), \
         patch.object(agent, "chat_async", _slow_chat):
        trace = await agent.run_agent(
            user_query="q", initial_context="", self_context="", history_block="",
        )

    assert "не ответил вовремя" in trace.answer
    assert trace.final_step == 0


@pytest.mark.asyncio
async def test_run_agent_llm_call_failed_sets_message():
    with patch.object(agent, "collect_tools", AsyncMock(return_value=[])), \
         patch.object(agent, "chat_async",
                       AsyncMock(side_effect=agent.LLMCallFailed("broker down"))):
        trace = await agent.run_agent(
            user_query="q", initial_context="", self_context="", history_block="",
        )

    assert "недоступен" in trace.answer
    assert "broker down" in trace.answer


@pytest.mark.asyncio
async def test_run_agent_recovers_from_malformed_json_then_answers():
    replies = [
        ("не JSON вообще", {"provider": "x", "cost_usd": 0.0}),
        (json.dumps({"action": "answer", "text": "восстановился"}),
         {"provider": "x", "cost_usd": 0.0}),
    ]

    with patch.object(agent, "collect_tools", AsyncMock(return_value=[])), \
         patch.object(agent, "chat_async", AsyncMock(side_effect=replies)):
        trace = await agent.run_agent(
            user_query="q", initial_context="", self_context="", history_block="",
            max_steps=3,
        )

    assert trace.answer == "восстановился"
    assert trace.steps[0]["error"] == "no JSON"


@pytest.mark.asyncio
async def test_run_agent_gives_up_after_max_steps():
    pending = json.dumps({"action": "tool", "name": "unknown_tool", "params": {}})

    with patch.object(agent, "collect_tools", AsyncMock(return_value=[])), \
         patch.object(agent, "chat_async",
                       AsyncMock(return_value=(pending, {"provider": "x", "cost_usd": 0.0}))):
        trace = await agent.run_agent(
            user_query="q", initial_context="", self_context="", history_block="",
            max_steps=2,
        )

    assert trace.answer  # some fallback message, not empty
    assert len(trace.steps) == 2
    assert trace.steps[0]["observation"]["error"].startswith("unknown tool")
