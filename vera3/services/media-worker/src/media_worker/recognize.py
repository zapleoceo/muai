"""Recognition side of media-worker: download, vision (broker), audio ASR.

Vision goes through the BROKER (aib.zapleo.com) like every other LLM call.
Audio is LOCAL-FIRST: asr-local (faster-whisper on the compose network)
handles voices for free; broker /v1/transcribe is the fallback when
asr-local is down or mid-redeploy. The broker's chronic 502/503
("no transcription key available") is why local goes first.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os

import httpx
from vera_shared.llm.client import LLMCallFailed, chat_async

log = logging.getLogger("media-worker")

TELEGRAM_TOOLS_URL = os.environ.get("TELEGRAM_TOOLS_URL", "http://ingestor-telegram:8000")
INTERNAL_SECRET = os.environ["INTERNAL_SECRET"]
BROKER_URL = os.environ.get("BROKER_URL", "").rstrip("/")
BROKER_PROJECT_KEY = os.environ.get("BROKER_PROJECT_KEY", "")
ASR_LOCAL_URL = os.environ.get("ASR_LOCAL_URL", "http://asr-local:8000").rstrip("/")
# Локальный whisper на 1 CPU-потоке жуёт длинные аудио дольше реального
# времени — ждём, не режем (см. политику таймаутов брокера).
ASR_LOCAL_TIMEOUT_S = int(os.environ.get("ASR_LOCAL_TIMEOUT_S", "1800"))
_MAX_AUDIO_BYTES = 25 * 1024 * 1024   # Whisper limit, mirror broker's guard
_EMPTY_TRANSCRIPT = "(тишина/неразборчиво)"


async def warm_entity_cache(attempts: int = 30, delay_s: float = 10.0) -> bool:
    """Prime Telethon's entity cache in the ingestor before downloads.

    A fresh session resolves rare peers only after seeing them once —
    /media/download on such a peer fails with "Could not find the input
    entity". One list_dialogs pass inside the ingestor process fills the
    cache. Best-effort with retries because the ingestor may still be
    booting when the worker starts.
    """
    for attempt in range(1, attempts + 1):
        try:
            async with httpx.AsyncClient(timeout=120) as c:
                r = await c.post(
                    f"{TELEGRAM_TOOLS_URL}/tools/list_dialogs",
                    json={"limit": 200},
                    headers={"X-Internal-Secret": INTERNAL_SECRET},
                )
            if r.status_code < 400:
                log.info("entity cache warmed: %s dialogs", r.json().get("count"))
                return True
            log.warning("warm-up try %s: HTTP %s", attempt, r.status_code)
        except httpx.HTTPError as e:
            log.warning("warm-up try %s: %s", attempt, e)
        if attempt < attempts:
            await asyncio.sleep(delay_s)
    log.warning("entity cache warm-up failed after %s tries — continuing", attempts)
    return False


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


async def _transcribe_local(audio_bytes: bytes, mime: str) -> str:
    async with httpx.AsyncClient(timeout=ASR_LOCAL_TIMEOUT_S) as c:
        r = await c.post(
            f"{ASR_LOCAL_URL}/transcribe",
            content=audio_bytes,
            headers={"Content-Type": mime or "audio/ogg"},
        )
    if r.status_code >= 400:
        raise RuntimeError(f"asr-local HTTP {r.status_code}: {r.text[:200]}")
    return (r.json().get("text") or "").strip()


async def _transcribe_broker(audio_bytes: bytes, mime: str) -> str:
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


async def _recognize_audio(audio_bytes: bytes, mime: str) -> tuple[str, str]:
    """Returns (text, source). asr-local first; broker as fallback.
    ASR_LOCAL_URL="" disables the local path entirely."""
    if len(audio_bytes) > _MAX_AUDIO_BYTES:
        raise RuntimeError(f"http 413: audio > {_MAX_AUDIO_BYTES // (1024 * 1024)}MB")
    if ASR_LOCAL_URL:
        try:
            txt = await _transcribe_local(audio_bytes, mime)
            return txt or _EMPTY_TRANSCRIPT, "local"
        except Exception as e:
            log.warning("asr-local failed (%s) — falling back to broker", e)
    txt = await _transcribe_broker(audio_bytes, mime)
    return txt or _EMPTY_TRANSCRIPT, "broker"


async def _process_one(row: dict) -> tuple[str, dict, str | None]:
    """Returns (new_text_segment, extra_metadata, error)."""
    meta = row["metadata"] or {}
    chat_id = meta.get("chat_id")
    msg_id = meta.get("msg_id")
    kind = meta.get("media_kind")
    if not chat_id or not msg_id or not kind:
        return "", {}, "missing chat_id/msg_id/media_kind in metadata"

    raw, mime, err = await _download(chat_id, msg_id)
    if err:
        return "", {}, f"download: {err}"

    if kind in {"photo", "sticker"}:
        try:
            txt = await _recognize_photo(base64.b64encode(raw).decode("ascii"),
                                         mime or "image/jpeg", event_id=row.get("id"))
        except Exception as e:
            return "", {}, f"vision: {e}"
        label = "recognized photo" if kind == "photo" else "recognized sticker"
        return f"\n--- {label} ---\n{txt}", {}, None

    if kind in {"voice", "audio"}:
        try:
            txt, source = await _recognize_audio(raw, mime or "audio/ogg")
        except Exception as e:
            return "", {}, f"whisper: {e}"
        label = "voice transcription" if kind == "voice" else "audio transcription"
        return f"\n--- {label} ---\n{txt}", {"media_recognition": f"ok_{source}"}, None

    return "", {}, f"unsupported media_kind: {kind}"
