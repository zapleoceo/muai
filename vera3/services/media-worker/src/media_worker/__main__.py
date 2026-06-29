"""media-worker — recognize photo (vision) + voice/audio (whisper).

Picks events with triage_status='media_pending', downloads media via
ingestor-telegram's /media/download, runs recognition, appends extracted
text to content_text, sets triage_status='pending' so normal triage picks
it up.

Providers (both DIRECT — the broker is text-only and 422s on media):
  - vision  → Gemini generateContent direct (GEMINI_API_KEY)
  - whisper → Groq audio/transcriptions direct (GROQ_API_KEY)

If a key is missing or recognition fails permanently, the event degrades:
its placeholder ([photo]/[voice: Ns]) stays and it enters normal triage,
so media is never lost — recognition is best-effort.

Failures policy:
  - Telethon-download fail (deleted msg, no access): mark 'error' with reason
  - Recognition fail: keep status='media_pending' (next iteration retries)
  - Hard size limit 25 MB (Whisper limit); larger files marked 'error'
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
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
# Vision goes DIRECT to Gemini — the broker (aib.zapleo.com) is text-only
# and 422s on multimodal content. Same reason whisper goes direct to Groq.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_VISION_MODEL = os.environ.get("GEMINI_VISION_MODEL", "gemini-2.0-flash")
POLL_S = int(os.environ.get("MEDIA_POLL_S", "10"))
BATCH = int(os.environ.get("MEDIA_BATCH", "3"))


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


async def _recognize_photo(image_b64: str, mime: str) -> str:
    """Vision DIRECT to Gemini generateContent (broker is text-only)."""
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not set")
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_VISION_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    payload = {
        "contents": [{
            "parts": [
                {"text": _VISION_PROMPT},
                {"inline_data": {"mime_type": mime or "image/jpeg", "data": image_b64}},
            ],
        }],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 400},
    }
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(url, json=payload)
    if r.status_code >= 400:
        raise RuntimeError(f"Gemini vision HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError) as e:
        # Safety block or empty — surface so we don't loop on it
        reason = data.get("candidates", [{}])[0].get("finishReason", "unknown")
        raise RuntimeError(f"Gemini vision no text (finishReason={reason})") from e


async def _recognize_audio(audio_bytes: bytes, mime: str) -> str:
    """Groq Whisper direct call (broker не пропускает audio multipart)."""
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY not set")
    suffix = ".ogg" if "ogg" in (mime or "") else ".mp3"
    files = {"file": (f"audio{suffix}", audio_bytes, mime or "audio/ogg")}
    data = {"model": "whisper-large-v3-turbo", "response_format": "text"}
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    async with httpx.AsyncClient(timeout=90) as c:
        r = await c.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            files=files, data=data, headers=headers,
        )
    if r.status_code >= 400:
        raise RuntimeError(f"Groq Whisper HTTP {r.status_code}: {r.text[:200]}")
    return r.text.strip()


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

    if kind == "photo":
        try:
            txt = await _recognize_photo(base64.b64encode(raw).decode("ascii"),
                                         mime or "image/jpeg")
        except Exception as e:
            return "", f"vision: {e}"
        return f"\n--- recognized photo ---\n{txt}", None

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
    """Config/permission errors — retrying won't help, degrade immediately."""
    e = err.lower()
    if "api_key not set" in e:                  # GROQ/GEMINI key missing
        return True
    if "no text (finishreason" in e:            # safety-blocked image
        return True
    # 4xx from provider = bad request / unauth (429 rate-limit is transient)
    return any(c in e for c in ("http 400", "http 401", "http 403", "http 404"))


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
