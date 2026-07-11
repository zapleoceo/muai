"""Semaphore-bounded wrappers around triage_one/triage_group_batch.

Both normalize to the same [(event_id, status, metadata, error)] shape
so process_pending() can flatten single-event and group-batch results
and handle them with one code path. The Semaphore instance itself is
owned by process_pending() (shared across both single and group-chunk
tasks in the same asyncio.gather) — these functions only borrow it.
"""
from __future__ import annotations

import asyncio
import logging

from vera_shared.db.models import EventRow
from vera_shared.llm.client import LLMCallFailed

from brain_triage.triage_calls import triage_group_batch, triage_one

log = logging.getLogger(__name__)


async def _process_one_with_sem(
    sem: asyncio.Semaphore, row: EventRow,
) -> list[tuple[int, str, dict | None, str | None]]:
    """Triage под семафором. Возвращает [(event_id, status, metadata, error)] —
    список из ОДНОГО элемента, чтобы форма результата совпадала с
    _process_group_chunk_with_sem() и обе ветки flatten'ились одним кодом
    в process_pending()."""
    async with sem:
        try:
            metadata = await asyncio.wait_for(triage_one(row), timeout=120)
            return [(row.id, "done", metadata, None)]
        except asyncio.TimeoutError:
            return [(row.id, "pending", None, "timeout")]
        except LLMCallFailed as e:
            return [(row.id, "pending", None, str(e)[:200])]
        except Exception as e:
            log.warning("Triage failed for event %s: %s", row.id, e)
            return [(row.id, "error", None, str(e)[:500])]


async def _process_group_chunk_with_sem(
    sem: asyncio.Semaphore, chunk: list[EventRow],
) -> list[tuple[int, str, dict | None, str | None]]:
    """Один групповой batch (≤TRIAGE_GROUP_BATCH_SIZE событий) под семафором —
    ОДИН LLM-вызов на весь chunk. Возвращает список результатов, по одному
    на событие — событие которое LLM не вернула (partial response) идёт
    обратно в pending на одиночный ретрай, не теряется."""
    async with sem:
        try:
            by_id = await asyncio.wait_for(
                triage_group_batch(chunk), timeout=180,
            )
            out = []
            for row in chunk:
                metadata = by_id.get(row.id)
                if metadata is None:
                    out.append((row.id, "pending", None,
                                "missing from batch LLM response"))
                else:
                    out.append((row.id, "done", metadata, None))
            return out
        except asyncio.TimeoutError:
            return [(row.id, "pending", None, "timeout") for row in chunk]
        except LLMCallFailed as e:
            return [(row.id, "pending", None, str(e)[:200]) for row in chunk]
        except Exception as e:
            log.warning("Group batch triage failed (%d events): %s",
                        len(chunk), e)
            return [(row.id, "error", None, str(e)[:500]) for row in chunk]
