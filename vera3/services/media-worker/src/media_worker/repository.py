"""Queue side of media-worker: claim/lease, finalize, retry policy."""
from __future__ import annotations

import json
import logging
import os

from sqlalchemy import text
from vera_shared.control import is_backfill_paused, reserve_backfill_allowance
from vera_shared.db.engine import get_session

log = logging.getLogger("media-worker")

BATCH = int(os.environ.get("MEDIA_BATCH", "3"))
MAX_MEDIA_RETRIES = 3
BACKOFF_MIN = [2, 15, 60]   # minutes for retry 1, 2, 3


def _is_permanent(err: str) -> bool:
    """Errors where retrying won't help — degrade immediately instead of
    burning the backoff budget."""
    e = err.lower()
    if "broker_url" in e:                        # broker not configured
        return True
    if "empty text" in e:                        # vision safety-block / blank
        return True
    if "too large" in e or "timed out" in e:     # oversize never fits; a file
        return True                              # that hangs will hang again
    # NOTE: 503 "no provider available" is TRANSIENT, not permanent — it
    # happens when all gemini keys are momentarily in cooldown (free-tier
    # rate-limit churn). They recover within minutes, so the backoff retry
    # (2m/15m/60m) catches a live key. Degrading here would lose the image.
    # Broker/client 4xx = bad request / scope / payload-too-large.
    # 429 (rate-limit) and 5xx (broker/provider down) stay transient → retry.
    return any(c in e for c in (
        "http 400", "http 401", "http 403", "http 404", "http 413",
    ))


async def _claim_batch(limit: int = BATCH) -> list[dict]:
    """Claim media_pending whose retry window is due.

    media_next_retry_at lives in metadata (jsonb) — NULL means never tried.
    Filtering by it means a failed event with a future retry time is SKIPPED,
    so the queue advances instead of looping on the first N forever.
    `limit` is trimmed by the backfill rate limiter.
    """
    if limit <= 0:
        return []
    # Атомарный lease-claim: одним UPDATE проставляем media_next_retry_at на
    # 10 мин вперёд у выбранных строк и их же возвращаем. Это (а) не даёт
    # второму инстансу/следующему поллу забрать те же события (claim атомарен
    # со сменой состояния, чего SELECT FOR UPDATE в отдельной транзакции не
    # давал), (б) если finalize упадёт — строка не зациклится, лиз оттолкнёт
    # следующую попытку на 10 мин.
    async with get_session() as s:
        rs = (await s.execute(text("""
            UPDATE events SET metadata = jsonb_set(
                COALESCE(metadata, '{}'::jsonb),
                '{media_next_retry_at}',
                to_jsonb((NOW() + interval '10 minutes')::text)
            )
            WHERE id IN (
                SELECT id FROM events
                WHERE triage_status = 'media_pending'
                  AND (
                    metadata->>'media_next_retry_at' IS NULL
                    OR (metadata->>'media_next_retry_at')::timestamp < NOW()
                  )
                -- newest-first: живые новые медиа (высший id) всегда впереди
                -- переобрабатываемого бэклога (низкий id), поэтому массовый
                -- requeue старых провалов не морозит распознавание свежих.
                ORDER BY id DESC
                FOR UPDATE SKIP LOCKED
                LIMIT :lim
            )
            RETURNING id, content_text, metadata
        """), {"lim": limit})).mappings().all()
    return [dict(r) for r in rs]


async def _on_success(event_id: int, append: str, extra_meta: dict | None = None) -> None:
    """Append recognized text + merge extra metadata (e.g. how the voice
    was recognized: media_recognition=ok_local|ok_broker).

    Guard triage_status: воркер, переживший 10-мин lease (другой инстанс уже
    обработал и перевёл в pending), не должен приклеить текст ВТОРОЙ раз."""
    async with get_session() as s:
        res = await s.execute(text("""
            UPDATE events
            SET content_text = content_text || :app,
                triage_status = 'pending',
                triage_error = NULL,
                metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:extra AS jsonb)
            WHERE id = :id AND triage_status = 'media_pending'
        """), {"app": append, "extra": json.dumps(extra_meta or {}),
               "id": event_id})
    if (res.rowcount or 0) == 0:
        log.warning("media %s: already finalized elsewhere — append skipped", event_id)


def _plan_failure(meta: dict | None, err: str) -> dict:
    """Pure decision: given prior metadata + an error, decide degrade-vs-retry.

    Returns a plan dict (no DB, no clock) so it's unit-testable:
      degrade=True            → hand to normal triage now (placeholder kept)
      degrade=False           → schedule a backoff retry
      retry_count, backoff_min, action(for logs)
    Degrade when the error is permanent OR the next attempt would be the
    Nth (MAX_MEDIA_RETRIES)."""
    retries = int((meta or {}).get("media_retry_count", 0))
    permanent = _is_permanent(err)
    if permanent or retries + 1 >= MAX_MEDIA_RETRIES:
        return {
            "degrade": True,
            "action": "degraded(permanent)" if permanent else "degraded",
            "retry_count": retries,
            "backoff_min": 0,
        }
    backoff = BACKOFF_MIN[min(retries, len(BACKOFF_MIN) - 1)]
    return {
        "degrade": False,
        "action": f"retry#{retries + 1} in {backoff}m",
        "retry_count": retries + 1,
        "backoff_min": backoff,
    }


async def _on_failure(event_id: int, meta: dict, err: str) -> str:
    """Apply the failure plan. Degraded events keep their placeholder
    ([photo]/[voice: Ns]) and go to 'pending' so they still enter the brain —
    recognition is best-effort. Returns the action taken for logging."""
    plan = _plan_failure(meta, err)

    if plan["degrade"]:
        async with get_session() as s:
            await s.execute(text("""
                UPDATE events
                SET triage_status = 'pending',
                    triage_error = :err,
                    metadata = jsonb_set(
                      COALESCE(metadata, '{}'::jsonb),
                      '{media_recognition}', '"failed"'
                    )
                WHERE id = :id AND triage_status = 'media_pending'
            """), {"err": err[:300], "id": event_id})
        return plan["action"]

    # Backoff retry. retry_count bound as text → cast to int inside to_jsonb
    # via CAST() (NOT '::int' — SQLAlchemy text() mangles '::' next to a bind).
    # next_retry_at computed server-side with make_interval(mins => …).
    async with get_session() as s:
        await s.execute(text("""
            UPDATE events
            SET triage_error = :err,
                metadata = jsonb_set(
                  jsonb_set(
                    COALESCE(metadata, '{}'::jsonb),
                    '{media_retry_count}', to_jsonb(CAST(:cnt AS integer))
                  ),
                  '{media_next_retry_at}',
                  to_jsonb(
                    (NOW() + make_interval(mins => CAST(:backoff AS integer)))::text
                  )
                )
            WHERE id = :id AND triage_status = 'media_pending'
        """), {"err": err[:300], "cnt": plan["retry_count"],
               "backoff": plan["backoff_min"], "id": event_id})
    return plan["action"]


async def _claim_limit() -> int:
    """How many media events this cycle may claim. 0 = skip (paused or rate
    budget spent); else the batch size capped by the even-tempo allowance."""
    if await is_backfill_paused():
        return 0
    granted = await reserve_backfill_allowance(BATCH)
    if granted is None:
        return BATCH
    return granted
