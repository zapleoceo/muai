"""media-worker — recognize photo (vision) + voice/audio (whisper).

Picks events with triage_status='media_pending', downloads media via
ingestor-telegram's /media/download, runs recognition, appends extracted
text to content_text, sets triage_status='pending' so normal triage picks
it up.

Providers:
  - vision  → broker chat() with capability='vision' (Gemini Flash etc.)
  - whisper → Groq API direct (GROQ_API_KEY); broker doesn't passthrough audio

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
from vera_shared.llm.client import LLMCallFailed, chat

log = logging.getLogger("media-worker")

TELEGRAM_TOOLS_URL = os.environ.get("TELEGRAM_TOOLS_URL", "http://ingestor-telegram:8000")
INTERNAL_SECRET = os.environ["INTERNAL_SECRET"]
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
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


async def _recognize_photo(image_b64: str, mime: str) -> str:
    """Vision via broker — OCR + caption in one prompt."""
    prompt = (
        "Опиши изображение по-русски в 1-3 коротких предложениях. "
        "Если на нём есть читаемый текст — приведи его дословно после метки `Текст:`. "
        "Если это скриншот UI/таблицы/чата — назови ключевые элементы (имена, числа, дата). "
        "Не выдумывай детали, которых не видно."
    )
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url",
             "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
        ],
    }]
    text_out, _meta = await chat(
        messages=messages,
        capability="vision",
        max_tokens=400,
        temperature=0.1,
        workflow="media_vision",
    )
    return text_out.strip()


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
        return "", f"missing chat_id/msg_id/media_kind in metadata"

    raw, mime, err = await _download(chat_id, msg_id)
    if err:
        return "", f"download: {err}"

    if kind == "photo":
        try:
            txt = await _recognize_photo(base64.b64encode(raw).decode("ascii"),
                                         mime or "image/jpeg")
        except LLMCallFailed as e:
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


async def _claim_batch() -> list[dict]:
    async with get_session() as s:
        rs = (await s.execute(text("""
            SELECT id, content_text, metadata
            FROM events
            WHERE triage_status = 'media_pending'
            ORDER BY id
            FOR UPDATE SKIP LOCKED
            LIMIT :lim
        """), {"lim": BATCH})).mappings().all()
    return [dict(r) for r in rs]


async def _finalize(event_id: int, append: str, new_status: str,
                     err: str | None) -> None:
    async with get_session() as s:
        if append:
            await s.execute(text("""
                UPDATE events
                SET content_text = content_text || :app,
                    triage_status = :st,
                    triage_error = :err
                WHERE id = :id
            """), {"app": append, "st": new_status, "err": err, "id": event_id})
        else:
            await s.execute(text("""
                UPDATE events
                SET triage_status = :st, triage_error = :err
                WHERE id = :id
            """), {"st": new_status, "err": err, "id": event_id})


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

            if err:
                # Hard failure → 'error' so triage doesn't loop forever.
                # Recoverable cases (broker down etc.) stay 'media_pending'
                # — distinguish by error prefix.
                if err.startswith(("vision:", "whisper:")):
                    new_status = "media_pending"   # retry next loop
                else:
                    new_status = "error"
                log.warning("event %s: %s", r["id"], err)
            else:
                new_status = "pending"   # normal triage takes over
                log.info("event %s: recognized %d chars",
                         r["id"], len(append))

            try:
                await _finalize(r["id"], append, new_status, err)
            except Exception as e:
                log.exception("finalize event %s failed: %s", r["id"], e)


if __name__ == "__main__":
    asyncio.run(main_loop())
