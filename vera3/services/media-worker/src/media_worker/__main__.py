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
        # Circuit breaker: vision-пул мёртв / бюджет капнут — не клеймим медиа
        # (иначе жжём retry-бюджет об заведомые «no provider available»).
        cooldown = await llm_cooldown_remaining_s("vision")
        if cooldown > 0:
            log.info("LLM circuit open for vision (%.0f min left) — idle",
                     cooldown / 60)
            await asyncio.sleep(min(cooldown, 60))
            continue
        limit = await _claim_limit()
        if limit <= 0:
            await asyncio.sleep(POLL_S)   # paused or rate budget spent
            continue
        try:
            rows = await _claim_batch(limit)
        except Exception as e:
            log.exception("claim failed: %s", e)
            await asyncio.sleep(POLL_S)
            continue

        if not rows:
            await asyncio.sleep(POLL_S)
            continue

        for r in rows:
            try:
                append, extra_meta, err = await _process_one(r)
            except Exception as e:
                append, extra_meta, err = "", {}, f"unexpected: {type(e).__name__}: {e}"

            try:
                if err:
                    action = await _on_failure(r["id"], r.get("metadata") or {}, err)
                    log.warning("event %s: %s → %s", r["id"], err, action)
                else:
                    await _on_success(r["id"], append, extra_meta)
                    log.info("event %s: recognized %d chars (%s) → pending",
                             r["id"], len(append),
                             extra_meta.get("media_recognition", "ok"))
            except Exception as e:
                log.exception("finalize event %s failed: %s", r["id"], e)


if __name__ == "__main__":
    asyncio.run(main_loop())
