"""MCP Events: фоновый цикл доставки в lifespan (флаг, вызов deliver_due, пустой outbox)."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from vera_mcp import events_loop, events_net

pytestmark = pytest.mark.asyncio


async def _stop(task: asyncio.Task) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_loop_calls_deliver_due(monkeypatch):
    deliver = AsyncMock(return_value=0)
    monkeypatch.setattr(events_loop, "deliver_due", deliver)
    monkeypatch.setattr(events_loop, "_prune", AsyncMock())
    monkeypatch.delenv("MCP_EVENTS_DELIVERY", raising=False)
    async with events_loop.delivery_running():
        for _ in range(100):
            if deliver.await_count:
                break
            await asyncio.sleep(0.01)
    assert deliver.await_count >= 1


async def test_flag_off_does_not_call(monkeypatch):
    deliver = AsyncMock(return_value=0)
    monkeypatch.setattr(events_loop, "deliver_due", deliver)
    monkeypatch.setenv("MCP_EVENTS_DELIVERY", "0")
    assert events_loop.delivery_enabled() is False
    async with events_loop.delivery_running():
        await asyncio.sleep(0.05)
    deliver.assert_not_awaited()


async def test_prune_runs_once_per_interval(monkeypatch):
    prune = AsyncMock()
    monkeypatch.setattr(events_loop, "deliver_due", AsyncMock(return_value=0))
    monkeypatch.setattr(events_loop, "_prune", prune)
    task = asyncio.create_task(events_loop.run_delivery_loop(poll_s=0.01))
    await asyncio.sleep(0.1)
    await _stop(task)
    assert prune.await_count == 1


async def test_loop_survives_iteration_error(monkeypatch):
    deliver = AsyncMock(side_effect=[RuntimeError("db down"), 0, 0, 0, 0, 0, 0, 0, 0, 0, 0])
    monkeypatch.setattr(events_loop, "deliver_due", deliver)
    monkeypatch.setattr(events_loop, "_prune", AsyncMock())
    task = asyncio.create_task(events_loop.run_delivery_loop(poll_s=0.01))
    await asyncio.sleep(0.1)
    await _stop(task)
    assert deliver.await_count >= 2


@pytest.mark.usefixtures("sqlite_db")
async def test_empty_outbox_makes_no_http(monkeypatch):
    def boom(_request):
        raise AssertionError("HTTP при пустом outbox")

    monkeypatch.setattr(events_net.net, "transport", httpx.MockTransport(boom))
    assert await events_loop.deliver_due() == 0
