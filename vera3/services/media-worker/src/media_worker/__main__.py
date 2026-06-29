"""media-worker — recognize photo (vision) + voice/audio (whisper).

Picks events with triage_status='media_pending', downloads media via
ingestor-telegram's /media/download, runs recognition, appends extracted
text to content_text, sets triage_status='pending' so normal triage picks
it up.

Recognition goes through the BROKER (aib.zapleo.com) like every other LLM
call in Vera — no provider keys live here:
  - vision  → POST /v1/chat?capability=vision  (multimodal content blocks)
  - whisper → POST /v1/transcribe              (multipart audio upload)

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
from vera_shared.db.engine import get_session, init_engine

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


async def _recognize_photo(image_b64: str, mime: str) -> str:
    """Vision via broker /v1/chat?capability=vision (multimodal content)."""
    payload = {
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": _VISION_PROMPT},
                {"type": "image_url", "image_url": {
                    "url": f"data:{mime or 'image/jpeg'};base64,{image_b64}"}},
            ],
        }],
        "max_tokens": 400,
        "temperature": 0.1,
        "workflow": "media_vision",
    }
    async with httpx.AsyncClient(timeout=90) as c:
        r = await c.post(
            f"{BROKER_URL}/v1/chat", params={"capability": "vision"},
            json=payload, headers=_broker_headers(),
        )
    if r.status_code >= 400:
        raise RuntimeError(f"broker vision HTTP {r.status_code}: {r.text[:200]}")
    txt = (r.json().get("text") or "").strip()
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
                                         mime or "image/jpeg")
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
    # Broker/client 4xx = bad request / scope / payload-too-large.
    # 429 (rate-limit) and 5xx (broker/provider down) stay transient → retry.
    return any(c in e for c in (
        "http 400", "http 401", "http 403", "http 404", "http 413",
    ))


async def _claim_batch() -> list[dict]:
    """Claim media_pending whose retry window is due.

    media_next_retry_at lives in metadata (jsonb) — NULL means never tried.
    Filtering by it means a failed event with a future retry time is SKIPPED,
    so the queue advances instead of looping on the first N forever.
    """
    async with get_session() as s:
        rs = (await s.execute(text("""
            SELECT id, content_text, metadata
            FROM events
            WHERE triage_status = 'media_pending'
              AND (
                metadata->>'media_next_retry_at' IS NULL
                OR (metadata->>'media_next_retry_at')::timestamp < NOW()
              )
            ORDER BY id
            FOR UPDATE SKIP LOCKED
            LIMIT :lim
        """), {"lim": BATCH})).mappings().all()
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


async def _on_failure(event_id: int, meta: dict, err: str) -> str:
    """Increment retry; schedule backoff or degrade to plain triage.

    Degraded events keep their placeholder ([photo]/[voice: Ns]) and go to
    'pending' so they still enter the brain — recognition was best-effort.
    Returns the action taken for logging.
    """
    retries = int((meta or {}).get("media_retry_count", 0))
    permanent = _is_permanent(err)

    if permanent or retries + 1 >= MAX_MEDIA_RETRIES:
        # Give up — placeholder already in content_text, hand to normal triage.
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
        return "degraded" if not permanent else "degraded(permanent)"

    backoff = BACKOFF_MIN[min(retries, len(BACKOFF_MIN) - 1)]
    async with get_session() as s:
        await s.execute(text(f"""
            UPDATE events
            SET triage_error = :err,
                metadata = jsonb_set(
                  jsonb_set(
                    COALESCE(metadata, '{{}}'::jsonb),
                    '{{media_retry_count}}', to_jsonb(:cnt)
                  ),
                  '{{media_next_retry_at}}',
                  to_jsonb((NOW() + INTERVAL '{backoff} minutes')::text)
                )
            WHERE id = :id
        """), {"err": err[:300], "cnt": retries + 1, "id": event_id})
    return f"retry#{retries + 1} in {backoff}m"


async def main_loop() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await init_engine()
    log.info("media-worker started, poll=%ss batch=%s", POLL_S, BATCH)

    while True:
        try:
            rows = await _claim_batch()
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
