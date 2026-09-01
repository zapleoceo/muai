"""Фоновый rel-extract ограничен по числу одновременных задач и по времени.

Задачи создаются fire-and-forget и не ожидаются, а семафор в
process_pending() пересоздаётся на каждый вызов и ограничивает только
передний план — поэтому между вызовами фоновые задачи копились без
потолка. Каждая при этом открывает до десятка сессий к пулу из 10
соединений, общему с claim'ом, записью статусов, watchdog и retry-циклом.
"""
from __future__ import annotations

import asyncio

import pytest
from brain_triage import background_loops as bl
from brain_triage.config import REL_EXTRACT_CONCURRENCY, REL_EXTRACT_TIMEOUT_S


@pytest.fixture(autouse=True)
def _fresh_semaphore():
    bl._rel_sem = None
    bl._bg_tasks.clear()
    yield
    bl._rel_sem = None


@pytest.mark.asyncio
async def test_concurrency_is_capped(monkeypatch):
    """Больше REL_EXTRACT_CONCURRENCY задач одновременно в работу не уходит."""
    live = 0
    peak = 0
    release = asyncio.Event()

    async def _slow(event_id: int, body: str) -> None:
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        try:
            await release.wait()
        finally:
            live -= 1

    monkeypatch.setattr("vera_shared.graph.rel_extract.extract_and_store", _slow)

    tasks = [asyncio.create_task(bl._safe_rel_extract(i, "x" * 50)) for i in range(12)]
    await asyncio.sleep(0.05)          # дать всем, кому дадут, стартовать
    assert peak == REL_EXTRACT_CONCURRENCY, f"в работе {peak}, потолок {REL_EXTRACT_CONCURRENCY}"

    release.set()
    await asyncio.gather(*tasks)
    assert peak == REL_EXTRACT_CONCURRENCY


@pytest.mark.asyncio
async def test_slow_extraction_is_timed_out_not_left_hanging(monkeypatch, caplog):
    """У переднего плана wait_for есть (concurrency.py), у фонового не было:
    задача жила до брокерского потолка и всё это время держала слот."""
    started = asyncio.Event()

    async def _hang(event_id: int, body: str) -> None:
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr("vera_shared.graph.rel_extract.extract_and_store", _hang)
    monkeypatch.setattr(bl, "REL_EXTRACT_TIMEOUT_S", 0.05)

    with caplog.at_level("WARNING"):
        await bl._safe_rel_extract(1, "x" * 50)

    assert started.is_set()
    assert "таймаут" in caplog.text


@pytest.mark.asyncio
async def test_timeout_frees_the_slot(monkeypatch):
    """Отвалившаяся по таймауту задача обязана отпустить семафор, иначе
    потолок со временем схлопнется в ноль."""
    async def _hang(event_id: int, body: str) -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr("vera_shared.graph.rel_extract.extract_and_store", _hang)
    monkeypatch.setattr(bl, "REL_EXTRACT_TIMEOUT_S", 0.02)

    await asyncio.gather(*[bl._safe_rel_extract(i, "x" * 50)
                           for i in range(REL_EXTRACT_CONCURRENCY * 2)])

    sem = bl._rel_semaphore()
    assert not sem.locked(), "слоты не вернулись"


@pytest.mark.asyncio
async def test_failure_still_never_breaks_triage(monkeypatch, caplog):
    async def _boom(event_id: int, body: str) -> None:
        raise RuntimeError("broker down")

    monkeypatch.setattr("vera_shared.graph.rel_extract.extract_and_store", _boom)
    with caplog.at_level("WARNING"):
        await bl._safe_rel_extract(7, "x" * 50)   # не бросает
    assert "граф не построен" in caplog.text


def test_timeout_is_above_broker_deadline():
    """Свой таймаут должен быть не жёстче брокерского, иначе он будет резать
    нормальные вызовы вместо зависших."""
    from vera_shared.llm.broker_client import JOB_POLL_DEADLINE_S
    assert REL_EXTRACT_TIMEOUT_S >= JOB_POLL_DEADLINE_S


@pytest.mark.asyncio
async def test_same_name_resolved_once_per_event(sqlite_db, monkeypatch):
    """Один человек обычно встречается в нескольких фактах подряд. Каждый
    resolve — своя сессия, поэтому в пределах события он должен искаться раз."""
    import json

    from vera_shared.graph import rel_extract as rx

    calls: list[str] = []

    async def _fake_resolve(name: str):
        calls.append(name)
        return 42

    monkeypatch.setattr(rx, "resolve_entity_exact", _fake_resolve)
    monkeypatch.setattr(rx, "upsert_relationship", lambda **kw: _true())
    monkeypatch.setattr(rx, "chat_async", _reply(json.dumps({"relationships": [
        {"subject": "Игорь", "predicate": "works_at", "object": "Sintegrum",
         "confidence": 0.8, "fact": "a"},
        {"subject": "Игорь", "predicate": "coworker_of", "object": "Олег",
         "confidence": 0.8, "fact": "b"},
    ]})))

    await rx.extract_and_store(1, "Игорь работает в Sintegrum вместе с Олегом, давно")

    assert calls.count("Игорь") == 1, f"повторный резолв: {calls}"
    assert sorted(set(calls)) == ["Sintegrum", "Игорь", "Олег"]


async def _true(**_kw) -> bool:
    return True


def _reply(payload: str):
    async def _call(**_kw):
        return payload, {}
    return _call
