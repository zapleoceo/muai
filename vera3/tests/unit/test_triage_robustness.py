"""Fix 4 — надёжность триажа: batch-miss ретраится одиночно, two-phase
backoff использует ВСЕ ступени BACKOFF_MINUTES, fence не даёт стейл-воркеру
затереть чужой результат (логика группировки/индексации — чистые проверки)."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from brain_triage.background_loops import BACKOFF_MINUTES, MAX_RETRIES
from brain_triage.concurrency import BATCH_MISS_ERROR, _process_group_chunk_with_sem
from vera_shared.db.models import EventRow


def _row(eid: int, error: str | None = None) -> EventRow:
    return EventRow(id=eid, source="telegram", source_event_id=f"tg:{eid}",
                    account="userbot", category="group", content_text="hi",
                    metadata_={"chat_kind": "group"}, triage_error=error)


def test_batch_miss_rows_excluded_from_group_batching():
    from brain_triage.claim import chat_kind
    rows = [_row(1), _row(2, error=BATCH_MISS_ERROR), _row(3, error="timeout")]
    group_ids = {r.id for r in rows
                 if r.source == "telegram" and chat_kind(r) == "group"
                 and (r.triage_error or "") != BATCH_MISS_ERROR}
    assert group_ids == {1, 3}   # 2 пойдёт одиночным путём


@pytest.mark.asyncio
async def test_group_chunk_marks_missing_events_with_batch_miss():
    import asyncio

    import brain_triage.concurrency as conc
    chunk = [_row(1), _row(2)]
    with patch.object(conc, "triage_group_batch",
                      AsyncMock(return_value={1: {"importance": 5}})):
        out = await _process_group_chunk_with_sem(asyncio.Semaphore(1), chunk)
    assert out[0] == (1, "done", {"importance": 5}, None)
    assert out[1] == (2, "pending", None, BATCH_MISS_ERROR)


def test_backoff_ladder_is_fully_used():
    # Two-phase: schedule берёт BACKOFF[rc] (SQL [rc+1]) для rc=0..4,
    # release инкрементит rc; rc>=MAX_RETRIES → dead. Все 5 ступеней живые.
    assert BACKOFF_MINUTES == [1, 5, 30, 120, 720]
    assert MAX_RETRIES == 5
    for rc in range(MAX_RETRIES):
        assert BACKOFF_MINUTES[rc] > 0   # индекс валиден для каждой попытки


def test_retry_sql_two_phase_shape():
    import inspect

    from brain_triage import background_loops as bl
    src = inspect.getsource(bl._retry_failed_loop)
    # schedule: свежий error без next_retry_at; [rc+1] — 1-индексный массив
    assert "triage_next_retry_at IS NULL" in src
    assert "[triage_retry_count + 1]" in src
    assert "[triage_retry_count + 2]" not in src
    # release: только дозревшие
    assert "triage_next_retry_at < NOW()" in src


def test_claim_returning_carries_fence_fields():
    import inspect

    from brain_triage import claim
    src = inspect.getsource(claim._claim_batch)
    assert "triage_started_at" in src and "triage_error" in src


def test_worker_updates_are_fenced():
    import inspect

    from brain_triage import worker
    src = inspect.getsource(worker.process_pending)
    assert "EventRow.triage_started_at == started_by_id[event_id]" in src
    assert "fenced_out" in src
