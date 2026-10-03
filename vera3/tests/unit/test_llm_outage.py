"""Брейкер на сбой брокера: 5xx/сеть → пауза с экспоненциальным ростом.

Инцидент: ~16 400 HTTP 500 за 77 минут от двух реплик триажа, потому что
ошибки класса «other» нигде не записывались."""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import pytest
from vera_shared.llm import outage
from vera_shared.llm.broker_client import BrokerCallFailed
from vera_shared.llm.circuit import (
    classify_broker_error,
    llm_cooldown_remaining_s,
    note_llm_failure,
    reset_llm_cooldown,
)
from vera_shared.llm.client import LLMCallFailed, LLMCoolingDown, chat_async, embed


@pytest.fixture(autouse=True)
def _clean():
    outage.reset_outage()
    yield
    outage.reset_outage()


def test_is_outage_error():
    assert outage.is_outage_error("broker 500: Internal Server Error")
    assert outage.is_outage_error("broker 503: <html>")
    assert outage.is_outage_error("broker poll 502: bad gateway")
    assert outage.is_outage_error("broker network: ConnectTimeout")
    assert not outage.is_outage_error("broker 400: bad request")
    assert not outage.is_outage_error("broker 429: slow down")
    assert not outage.is_outage_error("job 5 failed: model said no")
    assert not outage.is_outage_error("")


def test_pause_grows_and_caps():
    assert [outage.pause_for(i) for i in range(6)] == [30, 60, 120, 240, 300, 300]


def test_opens_only_after_threshold():
    for _ in range(outage.OUTAGE_THRESHOLD - 1):
        outage.note_broker_outage("broker 500: x")
    assert outage.outage_remaining_s() == 0
    outage.note_broker_outage("broker 500: x")
    assert 0 < outage.outage_remaining_s() <= outage.BASE_PAUSE_S


def test_failures_while_open_do_not_escalate():
    for _ in range(outage.OUTAGE_THRESHOLD):
        outage.note_broker_outage("broker 500: x")
    first = outage.outage_remaining_s()
    for _ in range(50):
        outage.note_broker_outage("broker 500: x")
    assert outage.outage_remaining_s() <= first


def test_probe_failure_after_window_doubles_pause(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(outage.time, "monotonic", lambda: clock[0])
    for _ in range(outage.OUTAGE_THRESHOLD):
        outage.note_broker_outage("broker 500: x")
    clock[0] += outage.BASE_PAUSE_S + 1
    assert outage.outage_remaining_s() == 0
    outage.note_broker_outage("broker 500: x")
    assert outage.outage_remaining_s() == pytest.approx(2 * outage.BASE_PAUSE_S)


def test_success_closes(caplog):
    for _ in range(outage.OUTAGE_THRESHOLD):
        outage.note_broker_outage("broker 500: x")
    with caplog.at_level(logging.INFO, logger=outage.log.name):
        outage.note_broker_ok()
    assert outage.outage_remaining_s() == 0
    assert any("CLOSED" in r.message for r in caplog.records)


def test_logs_once_per_state_change(caplog):
    with caplog.at_level(logging.WARNING, logger=outage.log.name):
        for _ in range(100):
            outage.note_broker_outage("broker 500: x")
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_outage_not_classified_as_budget():
    assert classify_broker_error("broker 500: x") == "other"


@pytest.mark.asyncio
async def test_note_llm_failure_feeds_outage_for_every_capability():
    for _ in range(outage.OUTAGE_THRESHOLD):
        assert await note_llm_failure("embed", "broker 500: x") == "outage"
    with patch("vera_shared.llm.circuit.get_control", AsyncMock(return_value="")):
        assert await llm_cooldown_remaining_s("chat:fast") > 0
        assert await llm_cooldown_remaining_s("vision") > 0


@pytest.mark.asyncio
async def test_non_outage_error_changes_nothing():
    assert await note_llm_failure("chat:fast", "broker 400: nope") == "other"
    assert outage.outage_remaining_s() == 0


@pytest.mark.asyncio
async def test_reset_llm_cooldown_closes_outage():
    for _ in range(outage.OUTAGE_THRESHOLD):
        outage.note_broker_outage("broker 500: x")
    with patch("vera_shared.llm.circuit.get_control", AsyncMock(return_value="")):
        await reset_llm_cooldown("chat:fast")
    assert outage.outage_remaining_s() == 0


@pytest.mark.asyncio
async def test_client_stops_calling_broker_during_outage():
    """Главное: после порога вызовы не доходят до брокера вовсе."""
    broker = AsyncMock(side_effect=BrokerCallFailed("broker 500: boom"))
    with (patch("vera_shared.llm.client.broker_enabled", return_value=True),
          patch("vera_shared.llm.client.chat_async_via_broker", broker),
          patch("vera_shared.llm.client.embed_via_broker", broker),
          patch("vera_shared.llm.circuit.get_control", AsyncMock(return_value="")),
          patch("vera_shared.llm.circuit.set_control", AsyncMock())):
        for i in range(outage.OUTAGE_THRESHOLD):
            call = chat_async if i % 2 == 0 else None
            with pytest.raises(LLMCallFailed):
                await (call([{"role": "user", "content": "x"}]) if call else embed("x"))
        calls_at_open = broker.await_count
        for _ in range(20):
            with pytest.raises(LLMCoolingDown):
                await chat_async([{"role": "user", "content": "x"}])
            with pytest.raises(LLMCoolingDown):
                await embed("x")
        assert broker.await_count == calls_at_open
