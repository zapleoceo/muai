"""media-worker — recognize photo (vision) + voice/audio (whisper).

Picks events with triage_status='media_pending', downloads media via
ingestor-telegram's /media/download, runs recognition, appends extracted
text to content_text, sets triage_status='pending' so normal triage picks
it up.

Recognition goes through the BROKER (aib.zapleo.com) like every other LLM
call in Vera — no provider keys live here:
  - vision  → chat_async() submit+poll (/v1/jobs?capability=vision),
              multimodal content blocks — see vera_shared.llm.client
  - whisper → POST /v1/transcribe (multipart audio upload) — stays sync,
              transcription isn't on the broker's async-job capability list

The broker handles key selection, free-first routing, cost guard and
cooldowns. If recognition fails permanently the event degrades: its
placeholder ([photo]/[voice: Ns]) stays and it enters normal triage, so
media is never lost — recognition is best-effort.

Failures policy:
  - Telethon-download fail (deleted msg, no access): backoff retry → degrade
  - Recognition fail: backoff retry (media_next_retry_at), then degrade
  - Hard size limit 25 MB (Whisper limit); larger files degrade
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os

import httpx
from sqlalchemy import text
from vera_shared.control import is_backfill_paused, reserve_backfill_allowance
from vera_shared.db.engine import get_session, init_engine
from vera_shared.llm.client import LLMCallFailed, chat_async

log = logging.getLogger("media-worker")

TELEGRAM_TOOLS_URL = os.environ.get("TELEGRAM_TOOLS_URL", "http://ingestor-telegram:8000")
INTERNAL_SECRET = os.environ["INTERNAL_SECRET"]
BROKER_URL = os.environ.get("BROKER_URL", "").rstrip("/")
BROKER_PROJECT_KEY = os.environ.get("BROKER_PROJECT_KEY", "")
POLL_S = int(os.environ.get("MEDIA_POLL_S", "10"))
BATCH = int(os.environ.get("MEDIA_BATCH", "3"))
_MAX_AUDIO_BYTES = 25 * 1024 * 1024   # Whisper limit, mirror broker's guard


async def _download(chat_id: int, msg_id: int) -> tuple[bytes | None, str | None, str | None]:
    """Returns (bytes, mime, error)."""
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(
            f"{TELEGRAM_TOOLS_URL}/media/download",
            json={"chat_id": chat_id, "msg_id": msg_id},
            headers={"X-Internal-Secret": INTERNAL_SECRET},
        )
    if r.status_code >= 400:
        return None, None, f"HTTP {r.status_code}: {r.text[:200]}"
    data = r.json()
    if "error" in data:
        return None, None, data["error"]
    return base64.b64decode(data["b64"]), data.get("mime"), None


_VISION_PROMPT = (
    "Опиши изображение по-русски в 1-3 коротких предложениях. "
    "Если на нём есть читаемый текст — приведи его дословно после метки `Текст:`. "
    "Если это скриншот UI/таблицы/чата — назови ключевые элементы (имена, числа, дата). "
    "Не выдумывай детали, которых не видно."
)


def _broker_headers() -> dict[str, str]:
    if not (BROKER_URL and BROKER_PROJECT_KEY):
        raise RuntimeError("BROKER_URL/BROKER_PROJECT_KEY not set")
    return {"X-Project-Key": BROKER_PROJECT_KEY}


async def _recognize_photo(image_b64: str, mime: str, event_id: int | None = None) -> str:
    """Vision via broker — async job (submit+poll /v1/jobs), multimodal content.
    Routed through the shared client so it's covered by usage_log mirroring
    like every other capability (vision calls used to bypass it entirely)."""
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": _VISION_PROMPT},
            {"type": "image_url", "image_url": {
                "url": f"data:{mime or 'image/jpeg'};base64,{image_b64}"}},
        ],
    }]
    try:
        txt, _meta = await chat_async(
            messages=messages, capability="vision", max_tokens=400,
            temperature=0.1, workflow="media_vision", event_id=event_id,
        )
    except LLMCallFailed as e:
        raise RuntimeError(f"broker vision: {e}") from e
    txt = txt.strip()
    if not txt:
        raise RuntimeError("broker vision returned empty text")
    return txt


async def _recognize_audio(audio_bytes: bytes, mime: str) -> str:
    """Whisper via broker /v1/transcribe (multipart upload)."""
    if len(audio_bytes) > _MAX_AUDIO_BYTES:
        raise RuntimeError(f"http 413: audio > {_MAX_AUDIO_BYTES // (1024 * 1024)}MB")
    suffix = ".ogg" if "ogg" in (mime or "") else ".mp3"
    files = {"file": (f"audio{suffix}", audio_bytes, mime or "audio/ogg")}
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(
            f"{BROKER_URL}/v1/transcribe", params={"workflow": "media_voice"},
            files=files, headers=_broker_headers(),
        )
    if r.status_code >= 400:
        raise RuntimeError(f"broker whisper HTTP {r.status_code}: {r.text[:200]}")
    return (r.json().get("text") or "").strip()


async def _process_one(row: dict) -> tuple[str, str | None]:
    """Returns (new_text_segment, error)."""
    meta = row["metadata"] or {}
    chat_id = meta.get("chat_id")
    msg_id = meta.get("msg_id")
    kind = meta.get("media_kind")
    if not chat_id or not msg_id or not kind:
        return "", "missing chat_id/msg_id/media_kind in metadata"

    raw, mime, err = await _download(chat_id, msg_id)
    if err:
        return "", f"download: {err}"

    if kind in {"photo", "sticker"}:
        try:
            txt = await _recognize_photo(base64.b64encode(raw).decode("ascii"),
                                         mime or "image/jpeg", event_id=row.get("id"))
        except Exception as e:
            return "", f"vision: {e}"
        label = "recognized photo" if kind == "photo" else "recognized sticker"
        return f"\n--- {label} ---\n{txt}", None

    if kind in {"voice", "audio"}:
        try:
            txt = await _recognize_audio(raw, mime or "audio/ogg")
        except Exception as e:
            return "", f"whisper: {e}"
        label = "voice transcription" if kind == "voice" else "audio transcription"
        return f"\n--- {label} ---\n{txt}", None

    return "", f"unsupported media_kind: {kind}"


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
                ORDER BY id
                FOR UPDATE SKIP LOCKED
                LIMIT :lim
            )
            RETURNING id, content_text, metadata
        """), {"lim": limit})).mappings().all()
    return [dict(r) for r in rs]


async def _on_success(event_id: int, append: str) -> None:
    async with get_session() as s:
        await s.execute(text("""
            UPDATE events
            SET content_text = content_text || :app,
                triage_status = 'pending',
                triage_error = NULL
            WHERE id = :id
        """), {"app": append, "id": event_id})


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
                WHERE id = :id
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
            WHERE id = :id
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


async def main_loop() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await init_engine()
    log.info("media-worker started, poll=%ss batch=%s", POLL_S, BATCH)

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
                append, err = await _process_one(r)
            except Exception as e:
                append, err = "", f"unexpected: {type(e).__name__}: {e}"

            try:
                if err:
                    action = await _on_failure(r["id"], r.get("metadata") or {}, err)
                    log.warning("event %s: %s → %s", r["id"], err, action)
                else:
                    await _on_success(r["id"], append)
                    log.info("event %s: recognized %d chars → pending",
                             r["id"], len(append))
            except Exception as e:
                log.exception("finalize event %s failed: %s", r["id"], e)


if __name__ == "__main__":
    asyncio.run(main_loop())
