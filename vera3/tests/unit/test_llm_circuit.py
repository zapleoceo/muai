"""vera_shared.llm.circuit — budget-cap circuit breaker.

app_control — реальная SQLite; классификация и тайминги — чистые функции;
интеграция с client.chat() — мок broker-слоя."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from vera_shared.db import models  # noqa: F401 — registers app_control on Base
from vera_shared.llm.circuit import (
    classify_broker_error,
    llm_cooldown_remaining_s,
    next_utc_midnight,
    note_llm_failure,
    reset_llm_cooldown,
)

# ─── чистые функции ─────────────────────────────────────────────────────────


def test_classify_broker_error():
    assert classify_broker_error(
        "broker async call failed: job 123 failed: daily budget cap reached — "
        "retry after 00:00 UTC") == "budget_cap"
    assert classify_broker_error(
        "no provider available for vision (gave up after 8 retries)") == "no_provider"
    assert classify_broker_error("job still pending after 240s") == "other"
    assert classify_broker_error("") == "other"
    assert classify_broker_error(None) == "other"


def test_next_utc_midnight():
    now = datetime(2026, 7, 16, 15, 30, tzinfo=timezone.utc)
    m = next_utc_midnight(now)
    assert m == datetime(2026, 7, 17, 0, 0, tzinfo=timezone.utc)
    # 23:59 → всё равно следующая полночь, не текущая минута
    late = datetime(2026, 7, 16, 23, 59, tzinfo=timezone.utc)
    assert next_utc_midnight(late) == datetime(2026, 7, 17, 0, 0, tzinfo=timezone.utc)


# ─── с реальной SQLite (app_control) ────────────────────────────────────────


@pytest_asyncio.fixture
async def db(sqlite_db):
    import vera_shared.db.engine as engine_mod
    from sqlalchemy import event
    engine = engine_mod._engine

    # set_control пишет raw-SQL с now() (Postgres) — даём SQLite аналог
    @event.listens_for(engine.sync_engine, "connect")
    def _register_now(dbapi_conn, _rec):
        dbapi_conn.create_function(
            "now", 0, lambda: datetime.now(timezone.utc).isoformat())

    # сбросить пул: соединение от create_all создано ДО регистрации listener'а
    await engine.dispose()
    yield sqlite_db


@pytest.mark.asyncio
async def test_budget_cap_opens_short_probe_not_until_midnight(db):
    """Регресс инцидента 2026-07-31: кап в 00:25 глушил vision на 23.5 часа.
    Брокер сообщает о капе КОНКРЕТНОГО ключа — блокировать capability до
    полуночи нельзя, ждём короткую пробу."""
    kind = await note_llm_failure("chat:fast", "daily budget cap reached — retry after 00:00 UTC")
    assert kind == "budget_cap"
    remaining = await llm_cooldown_remaining_s("chat:fast")
    assert 25 * 60 < remaining <= 30 * 60          # проба 30 мин, НЕ до полуночи
    # другая capability не затронута
    assert await llm_cooldown_remaining_s("vision") == 0


@pytest.mark.asyncio
async def test_budget_cap_never_overshoots_utc_midnight(db):
    """Если полночь ближе интервала пробы — ждём ровно до сброса капа."""
    import vera_shared.llm.circuit as circ
    fake_now = datetime(2026, 7, 31, 23, 50, tzinfo=timezone.utc)   # до полуночи 10 мин

    class _FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return fake_now

    with patch.object(circ, "datetime", _FakeDT):
        await note_llm_failure("vision", "daily budget cap reached")
    raw = await circ.get_control("llm_cooldown:vision", "")
    until = datetime.fromisoformat(raw)
    assert until == datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)  # ровно полночь


@pytest.mark.asyncio
async def test_no_provider_opens_for_configured_minutes(db):
    kind = await note_llm_failure("vision", "no provider available for vision (gave up)")
    assert kind == "no_provider"
    remaining = await llm_cooldown_remaining_s("vision")
    assert 25 * 60 < remaining <= 30 * 60          # дефолт 30 мин


@pytest.mark.asyncio
async def test_other_errors_do_not_open_circuit(db):
    kind = await note_llm_failure("chat:fast", "job 42 still pending after 240s")
    assert kind == "other"
    assert await llm_cooldown_remaining_s("chat:fast") == 0


@pytest.mark.asyncio
async def test_reset_closes_early(db):
    await note_llm_failure("chat:fast", "daily budget cap reached")
    assert await llm_cooldown_remaining_s("chat:fast") > 0
    await reset_llm_cooldown("chat:fast")
    assert await llm_cooldown_remaining_s("chat:fast") == 0


@pytest.mark.asyncio
async def test_expired_cooldown_reads_zero(db):
    from vera_shared.control import set_control
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    await set_control("llm_cooldown:chat:fast", past)
    assert await llm_cooldown_remaining_s("chat:fast") == 0


@pytest.mark.asyncio
async def test_garbage_cooldown_value_reads_zero(db):
    from vera_shared.control import set_control
    await set_control("llm_cooldown:chat:fast", "not-a-date")
    assert await llm_cooldown_remaining_s("chat:fast") == 0


# ─── интеграция с client.chat/chat_async ────────────────────────────────────


@pytest.mark.asyncio
async def test_chat_raises_cooling_down_without_touching_broker(db):
    import vera_shared.llm.client as client_mod
    from vera_shared.llm.client import LLMCallFailed, LLMCoolingDown, chat
    await note_llm_failure("chat:fast", "daily budget cap reached")

    with patch.object(client_mod, "chat_via_broker", AsyncMock()) as broker, \
         patch.object(client_mod, "broker_enabled", lambda: True), \
         pytest.raises(LLMCoolingDown) as exc:
        await chat(messages=[{"role": "user", "content": "hi"}])
    broker.assert_not_awaited()                     # джоба НЕ создана
    assert isinstance(exc.value, LLMCallFailed)     # старые except-ветки ловят


@pytest.mark.asyncio
async def test_chat_async_failure_opens_circuit_then_blocks(db):
    import vera_shared.llm.client as client_mod
    from vera_shared.llm.broker_client import BrokerCallFailed
    from vera_shared.llm.client import LLMCallFailed, LLMCoolingDown, chat_async

    boom = BrokerCallFailed("job 9 failed: daily budget cap reached — retry after 00:00 UTC")
    with patch.object(client_mod, "chat_async_via_broker",
                      AsyncMock(side_effect=boom)) as broker, \
         patch.object(client_mod, "broker_enabled", lambda: True):
        with pytest.raises(LLMCallFailed):
            await chat_async(messages=[{"role": "user", "content": "hi"}])
        assert broker.await_count == 1
        # второй вызов отбивается мгновенно, брокер больше не тронут
        with pytest.raises(LLMCoolingDown):
            await chat_async(messages=[{"role": "user", "content": "hi"}])
        assert broker.await_count == 1


@pytest.mark.asyncio
async def test_chat_success_resets_cooldown(db):
    import vera_shared.llm.client as client_mod
    from vera_shared.llm.client import chat
    # no_provider-кулдаун истёк бы через 30 мин, но успешный вызов закрывает сразу
    await note_llm_failure("chat:fast", "no provider available for chat:fast")
    await reset_llm_cooldown("chat:fast")           # имитация «пул ожил» вручную

    with patch.object(client_mod, "chat_via_broker",
                      AsyncMock(return_value=("ok", {}))), \
         patch.object(client_mod, "broker_enabled", lambda: True):
        answer, _ = await chat(messages=[{"role": "user", "content": "hi"}])
    assert answer == "ok"
    assert await llm_cooldown_remaining_s("chat:fast") == 0
