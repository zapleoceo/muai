"""Оркестрация process_pending() на настоящей БД: claim → embed → dispatch → write.

Ядро триажа до сих пор не исполнялось ни одним тестом — только соседние
чистые функции. Здесь LLM-часть и Postgres-специфичные запросы (claim,
project-override) подменены, а записи статусов и эмбеддингов идут в
настоящую сессию, поэтому проверяются реальные UPDATE'ы, fence и то, что
rel-extract запускается ПОД ССЫЛКОЙ, а не голым create_task.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from brain_triage import background_loops as bl
from brain_triage import worker
from brain_triage.config import REL_EXTRACT_MIN_IMPORTANCE
from sqlalchemy import select
from vera_shared.db.models import EventRow


async def _seed(get_session, **over) -> EventRow:
    spec = {
        "source": "telegram", "source_event_id": "tg:1", "account": "userbot",
        # chat_kind=private → одиночный путь. Групповые telegram-события
        # уходят в батч-ветку (см. group_ids в process_pending) и разбираются
        # другим вызовом — для проверки записи статусов это лишний слой.
        "category": "private", "content_text": "Игорь работает в Sintegrum",
        "occurred_at": datetime(2026, 9, 1, 10, 0), "triage_status": "processing",
        "triage_started_at": datetime(2026, 9, 1, 10, 0, 5),
        "metadata_": {"chat_kind": "private", "owner_participates": True},
    }
    spec.update(over)
    async with get_session() as s:
        row = EventRow(**spec)
        s.add(row)
        await s.flush()
        await s.refresh(row)
        s.expunge(row)
    return row


def _wire(monkeypatch, row: EventRow, *, result, vectors=None):
    """Подменить всё, что ходит наружу: claim (Postgres-only SQL), брокер и
    project-override. Запись статуса/эмбеддинга остаётся настоящей."""
    monkeypatch.setattr(worker, "is_backfill_paused", AsyncMock(return_value=False))
    monkeypatch.setattr(worker, "reserve_backfill_allowance", AsyncMock(return_value=None))
    monkeypatch.setattr(worker, "_claim_batch", AsyncMock(return_value=[row]))
    monkeypatch.setattr(worker, "_embed_batch",
                        AsyncMock(return_value=vectors if vectors is not None else [[0.1, 0.2]]))
    monkeypatch.setattr(worker, "apply_project_override", AsyncMock())
    monkeypatch.setattr(worker, "_process_one_with_sem",
                        AsyncMock(return_value=[result]))
    monkeypatch.setattr(worker, "PACE_BETWEEN_S", 0)


@pytest.mark.asyncio
async def test_done_result_writes_status_and_clears_fence(sqlite_db, monkeypatch):
    row = await _seed(sqlite_db)
    meta = {"importance": 80, "nature": "world_event", "project": "itstep"}
    _wire(monkeypatch, row, result=(row.id, "done", meta, None))
    monkeypatch.setattr(worker, "_safe_rel_extract", AsyncMock())

    assert await worker.process_pending() == 1

    async with sqlite_db() as s:
        got = (await s.execute(select(EventRow).where(EventRow.id == row.id))).scalar_one()
    assert got.triage_status == "done"
    assert got.importance == 80
    assert got.project == "itstep"
    assert got.triage_started_at is None
    assert got.triage_error is None


@pytest.mark.asyncio
async def test_rel_extract_task_is_tracked_not_dangling(sqlite_db, monkeypatch):
    """Ссылка на фоновую задачу — единственное, что не даёт GC её собрать."""
    row = await _seed(sqlite_db)
    _wire(monkeypatch, row,
          result=(row.id, "done", {"importance": 80, "nature": "world_event"}, None))

    started = asyncio.Event()

    async def _slow(eid: int, body: str) -> None:
        started.set()
        await asyncio.sleep(0.05)

    monkeypatch.setattr(worker, "_safe_rel_extract", _slow)
    bl._bg_tasks.clear()

    await worker.process_pending()

    assert len(bl._bg_tasks) == 1, "rel-extract запущен без ссылки"
    await asyncio.wait_for(started.wait(), timeout=1)
    await asyncio.gather(*list(bl._bg_tasks), return_exceptions=True)
    await asyncio.sleep(0)
    assert not bl._bg_tasks, "ссылка не снята после завершения"


@pytest.mark.parametrize(("importance", "spawns"), [
    (0, False),
    (3, False),    # старый порог: пропускал ~весь поток
    (59, False),
    (60, True),    # ровно порог — включительно
    (95, True),
])
@pytest.mark.asyncio
async def test_rel_extract_respects_importance_threshold(
    sqlite_db, monkeypatch, importance, spawns,
):
    """Порог importance — единственный фильтр между триажем и LLM-вызовом
    rel-extract. Шкала 0-100 (brain_triage/schemas.py, prompts.py)."""
    assert REL_EXTRACT_MIN_IMPORTANCE == 60
    row = await _seed(sqlite_db)
    _wire(monkeypatch, row,
          result=(row.id, "done", {"importance": importance, "nature": "world_event"}, None))
    monkeypatch.setattr(worker, "_safe_rel_extract", AsyncMock())
    bl._bg_tasks.clear()

    await worker.process_pending()

    assert bool(bl._bg_tasks) is spawns
    for t in list(bl._bg_tasks):
        t.cancel()


@pytest.mark.asyncio
async def test_channel_post_never_reaches_rel_extract(sqlite_db, monkeypatch):
    """Второй фильтр: посты вещательных каналов графу не нужны, даже если
    LLM выставила высокую важность (should_extract_relations)."""
    row = await _seed(sqlite_db, metadata_={"chat_kind": "channel"})
    _wire(monkeypatch, row,
          result=(row.id, "done", {"importance": 95, "nature": "world_event"}, None))
    monkeypatch.setattr(worker, "_safe_rel_extract", AsyncMock())
    bl._bg_tasks.clear()

    await worker.process_pending()

    assert not bl._bg_tasks


@pytest.mark.parametrize(("source", "nature"), [
    ("telegram", "world_event"),                # нет в таблице → фолбэк
    ("voice", "conversation_with_me"),          # есть в таблице
])
@pytest.mark.asyncio
async def test_error_result_gets_nature_from_source(sqlite_db, monkeypatch, source, nature):
    """nature детерминируема по источнику даже когда LLM не ответила."""
    row = await _seed(sqlite_db, source=source, source_event_id=f"{source}:1")
    _wire(monkeypatch, row, result=(row.id, "error", None, "boom"))
    monkeypatch.setattr(worker, "_safe_rel_extract", AsyncMock())

    assert await worker.process_pending() == 0

    async with sqlite_db() as s:
        got = (await s.execute(select(EventRow).where(EventRow.id == row.id))).scalar_one()
    assert got.triage_status == "error"
    assert got.triage_error == "boom"
    assert got.nature == nature
    assert got.nature == worker.NATURE_BY_SOURCE.get(source, "world_event")
    assert got.triage_started_at is None


@pytest.mark.asyncio
async def test_stale_result_is_fenced_out(sqlite_db, monkeypatch):
    """Watchdog вернул событие в pending, пока мы его считали. Наш результат
    не должен затирать новый claim: фенс матчит triage_started_at из RETURNING."""
    row = await _seed(sqlite_db)
    async with sqlite_db() as s:
        fresh = (await s.execute(select(EventRow).where(EventRow.id == row.id))).scalar_one()
        fresh.triage_started_at = datetime(2026, 9, 1, 11, 0, 0)   # чужой claim
        fresh.triage_status = "processing"

    _wire(monkeypatch, row, result=(row.id, "done", {"importance": 80}, None))
    monkeypatch.setattr(worker, "_safe_rel_extract", AsyncMock())

    await worker.process_pending()

    async with sqlite_db() as s:
        got = (await s.execute(select(EventRow).where(EventRow.id == row.id))).scalar_one()
    assert got.triage_status == "processing", "стейл-результат затёр чужой claim"
    assert got.importance is None


@pytest.mark.asyncio
async def test_paused_backfill_claims_nothing(sqlite_db, monkeypatch):
    monkeypatch.setattr(worker, "is_backfill_paused", AsyncMock(return_value=True))
    claim = AsyncMock()
    monkeypatch.setattr(worker, "_claim_batch", claim)

    assert await worker.process_pending() == 0
    claim.assert_not_awaited()
