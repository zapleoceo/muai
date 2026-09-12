"""media-worker — recognize photo (vision) + voice/audio (ASR) events.

Picks events with triage_status='media_pending', downloads media via
ingestor-telegram's /media/download, runs recognition, appends extracted
text to content_text, sets triage_status='pending' so normal triage picks
it up. Recognition is best-effort: failures degrade with the placeholder
kept ([photo]/[voice: Ns]), media is never lost.

Split per the ~200-line rule:
  recognize.py   — download + vision + whisper (both via broker)
  repository.py  — claim/lease, success/failure bookkeeping, retry policy
"""
from __future__ import annotations

import asyncio
import logging
import os

from vera_shared.db.engine import init_engine

from media_worker.recognize import _process_one, warm_entity_cache
from media_worker.repository import (
    BATCH,
    _claim_batch,
    _claim_limit,
    _on_failure,
    _on_success,
)

log = logging.getLogger("media-worker")
POLL_S = int(os.environ.get("MEDIA_POLL_S", "10"))


async def main_loop() -> None:  # pragma: no cover — glue, pieces unit-tested
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await init_engine()
    log.info("media-worker started, poll=%ss batch=%s", POLL_S, BATCH)
    await warm_entity_cache()

    from vera_shared.llm.circuit import llm_cooldown_remaining_s

    while True:
        # Circuit breaker: если vision-пул капнут — НЕ клеймим фото (жгли бы
        # retry-бюджет об заведомые «no provider»), но голосовые/аудио идут
        # через отдельный whisper-пул, который не капнут — их продолжаем брать.
        vision_cd = await llm_cooldown_remaining_s("vision")
        limit = await _claim_limit()
        if limit <= 0:
            await asyncio.sleep(POLL_S)   # paused or rate budget spent
            continue
        try:
            rows = await _claim_batch(limit, voice_only=vision_cd > 0)
        except Exception as e:
            log.exception("claim failed: %s", e)
            await asyncio.sleep(POLL_S)
            continue

        if not rows:
            # Vision капнут и голосовых в очереди нет — ждём конца кулдауна
            # (кусками ≤60с), иначе обычный poll.
            await asyncio.sleep(min(vision_cd, 60) if vision_cd > 0 else POLL_S)
            continue

        # Весь захваченный батч — параллельно. Последовательная обработка
        # упирала темп в ОДНО фото за раз: 145с на снимок → потолок 24 в час,
        # при том что локальная модель брокера (единственный слот, 150с) была
        # занята лишь 12% времени, а gemini отвечал за 3.3с (замер 12.09.2026,
        # 5 часов: local 75 ok, gemini 27 ok). Очередь ждала не брокера, а нас.
        await asyncio.gather(*(_handle_row(r) for r in rows))


async def _handle_row(row: dict) -> None:
    """Распознать одно событие и записать итог. Свои исключения не выпускает:
    в gather соседние строки батча не должны страдать друг за друга."""
    try:
        append, extra_meta, err = await _process_one(row)
    except Exception as e:
        append, extra_meta, err = "", {}, f"unexpected: {type(e).__name__}: {e}"

    try:
        if err:
            action = await _on_failure(row["id"], row.get("metadata") or {}, err,
                                       carry_meta=extra_meta)
            log.warning("event %s: %s → %s", row["id"], err, action)
        else:
            await _on_success(row["id"], append, extra_meta)
            log.info("event %s: recognized %d chars (%s) → pending",
                     row["id"], len(append),
                     extra_meta.get("media_recognition", "ok"))
    except Exception as e:
        log.exception("finalize event %s failed: %s", row["id"], e)


if __name__ == "__main__":
    asyncio.run(main_loop())
