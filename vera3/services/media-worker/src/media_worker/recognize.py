"""Recognition side of media-worker: download, vision (broker), audio ASR.

Vision and whisper both go through the BROKER (aib.zapleo.com) like every
other LLM call — no provider keys live here. Whisper is hosted broker-side
(2026-07-18); the local asr-local experiment was removed the same day.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os

import httpx
from vera_shared.llm.client import LLMCallFailed, LLMJobPending, chat_async

log = logging.getLogger("media-worker")

TELEGRAM_TOOLS_URL = os.environ.get("TELEGRAM_TOOLS_URL", "http://ingestor-telegram:8000")
INTERNAL_SECRET = os.environ["INTERNAL_SECRET"]
BROKER_URL = os.environ.get("BROKER_URL", "").rstrip("/")
BROKER_PROJECT_KEY = os.environ.get("BROKER_PROJECT_KEY", "")
_MAX_AUDIO_BYTES = 25 * 1024 * 1024   # Whisper limit, mirror broker's guard

# Сколько ждать ответа брокера на ОДНО фото. Дефолт клиента — 120с, и его
# не хватает: брокер умеет считать vision локально (`local/qwen3vl`), а это
# медленно. Замер за неделю: 42с минимум, 117с в среднем, 222с максимум —
# 5 из 8 локальных джоб перевалили за 120с. Брокер их досчитывал, а Вера
# уже сдавалась: работа сожжена, фото уходило в ретрай и через три круга
# деградировало. Потолок ставился с запасом к наблюдаемому максимуму (420с),
# но модель за неделю просела вдвое: 12.09.2026 медиана 120–180с, и 57 джоб
# за сутки (16% работы) упирались в 420 и выбрасывались — 6.6 часа воркера
# в мусор. Брошенная джоба хуже долгой: работа сгорает, а фото уходит в
# ретрай и через три круга деградирует. Лиз на claim (MEDIA_LEASE_MIN)
# обязан покрывать BATCH * этот дедлайн — фото в батче идут последовательно.
VISION_DEADLINE_S = float(os.environ.get("MEDIA_VISION_DEADLINE_S", "900"))
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


async def _recognize_photo(image_b64: str, mime: str, event_id: int | None = None,
                           resume_job_id: int | str | None = None) -> str:
    """Vision via broker — async job (submit+poll /v1/jobs), multimodal content.
    Routed through the shared client so it's covered by usage_log mirroring
    like every other capability (vision calls used to bypass it entirely).
    `resume_job_id` — джоба прошлой попытки, которую брокер ещё считал, когда
    мы сдались по дедлайну: возвращаемся за ней. LLMJobPending пробрасывается
    как есть — в нём job_id для следующей попытки."""
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
            poll_deadline_s=VISION_DEADLINE_S, resume_job_id=resume_job_id,
        )
    except LLMJobPending:
        raise
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
    return (r.json().get("text") or "").strip() or _EMPTY_TRANSCRIPT


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
                                         mime or "image/jpeg", event_id=row.get("id"),
                                         resume_job_id=meta.get("media_job_id"))
        except LLMJobPending as e:
            # Брокер ещё считает — запоминаем джобу, следующая попытка
            # вернётся за результатом, а не отправит фото заново.
            return "", {"media_job_id": e.job_id}, f"vision: {e}"
        except Exception as e:
            return "", {}, f"vision: {e}"
        label = "recognized photo" if kind == "photo" else "recognized sticker"
        # Метка успеха и у фото, не только у голосовых: по ней доливка
        # (media_requeue.top_up) отличает провал от сделанного, а замер остатка
        # (measure) считает прогресс. Без неё распознанное фото навсегда
        # оставалось «не сделанным» — 12.09.2026 сайт показывал 20 612 при
        # настоящих 10 517.
        return f"\n--- {label} ---\n{txt}", {"media_recognition": "ok_broker"}, None

    if kind in {"voice", "audio"}:
        try:
            txt = await _recognize_audio(raw, mime or "audio/ogg")
        except Exception as e:
            return "", {}, f"whisper: {e}"
        label = "voice transcription" if kind == "voice" else "audio transcription"
        return f"\n--- {label} ---\n{txt}", {"media_recognition": "ok_broker"}, None

    return "", {}, f"unsupported media_kind: {kind}"
