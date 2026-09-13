"""Куски длинных событий: нарезать, эмбеддить пачками, записать.

Шаг после записи вектора события (worker.process_pending). Идёт только для
событий, чей вектор записан в этом батче, и только когда накачена миграция
032. Короткие события сюда тоже попадают — но только чтобы снять куски,
оставшиеся от прежнего длинного текста (перезаписанная выжимка сессии).
Отказ эмбеддинга кусков не трогает триаж: вектор события уже записан.
"""
from __future__ import annotations

import logging

from vera_shared.db.chunk_vectors import (
    chunk_table_available,
    drop_event_chunks,
    replace_event_chunks,
)
from vera_shared.text_chunks import needs_chunks, split_chunks

from brain_triage.triage_calls import _embed_batch

log = logging.getLogger(__name__)

#: Кусков в одном запросе к брокеру. Батч триажа × 24 куска мог бы дать сотни
#: входов в одном запросе; 64 держит запрос далеко от лимитов Voyage.
EMBED_BATCH = 64


async def embed_event_chunks(events: list[tuple[int, str]]) -> int:
    """(event_id, content_text) с записанным вектором → сколько событий
    получили куски."""
    if not events or not await chunk_table_available():
        return 0
    long_events = [(eid, split_chunks(body)) for eid, body in events
                   if needs_chunks(body)]
    try:
        await drop_event_chunks([eid for eid, body in events if not needs_chunks(body)])
    except Exception as e:  # noqa: BLE001 — статусы триажа уже записаны, не роняем батч
        log.warning("снятие устаревших кусков не удалось: %s", e)
    texts = [piece for _, pieces in long_events for piece in pieces]
    vectors: list[list[float] | None] = []
    for i in range(0, len(texts), EMBED_BATCH):
        vectors.extend(await _embed_batch(texts[i:i + EMBED_BATCH]))

    stored = 0
    pos = 0
    for eid, pieces in long_events:
        got = vectors[pos:pos + len(pieces)]
        pos += len(pieces)
        if len(got) != len(pieces) or any(v is None for v in got):
            log.warning("куски события %s не эмбеддились — останется один вектор", eid)
            continue
        try:
            await replace_event_chunks(eid, [v for v in got if v is not None])
            stored += 1
        except Exception as e:  # noqa: BLE001 — одно битое событие не роняет остальные
            log.warning("запись кусков события %s не удалась: %s", eid, e)
    if long_events:
        log.info("куски: %d событий, %d кусков, записано %d",
                 len(long_events), len(texts), stored)
    return stored
