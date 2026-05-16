"""Enrich retrieved messages: fill transcription for voice/audio with no text."""
import logging

from sqlalchemy import text

from app.db.database import AsyncSessionLocal
from app.llm.embedding import transcribe_audio_gemini
from app.services.answering_types import RetrievedContext

logger = logging.getLogger(__name__)

_AUDIO_TYPES = {"voice", "audio"}
_MAX_LIVE = 5  # max live transcriptions per request


def _needs_transcription(msg: dict) -> bool:
    media = str(msg.get("media_type") or "").lower()
    if media not in _AUDIO_TYPES:
        return False
    text_val = str(msg.get("text") or "").strip()
    return not text_val or text_val in {f"[{media}]", "[voice]", "[audio]", "[media]"}


async def _lookup_chunk_transcription(chat_id: int, tg_msg_id: int) -> str | None:
    async with AsyncSessionLocal() as session:
        row = (await session.execute(
            text(
                "SELECT chunk_text, meta FROM media_chunks "
                "WHERE chat_id = :chat_id AND source_tg_msg_id = :tg_msg_id LIMIT 1"
            ),
            {"chat_id": chat_id, "tg_msg_id": tg_msg_id},
        )).mappings().first()
    if not row:
        return None
    import json
    meta = {}
    try:
        meta = json.loads(row["meta"] or "{}")
    except Exception:
        pass
    if meta.get("transcription"):
        return str(meta["transcription"])
    chunk = str(row["chunk_text"] or "").strip()
    # strip the header line "[Чат: ... | voice]" if present
    lines = chunk.splitlines()
    body = "\n".join(l for l in lines if not l.startswith("[Чат:")).strip()
    return body or None


async def _live_transcribe(chat_id: int, tg_msg_id: int) -> str | None:
    try:
        from app.userbot.client import get_client
        client = get_client()
        entity = await client.get_entity(chat_id)
        msgs = await client.get_messages(entity, ids=[tg_msg_id])
        tg = msgs[0] if msgs else None
        if tg is None or getattr(tg, "media", None) is None:
            return None
        raw = await client.download_media(tg, file=bytes)
        if not isinstance(raw, (bytes, bytearray)) or not raw:
            return None
        mime = getattr(getattr(tg, "file", None), "mime_type", None) or "audio/ogg"
        return await transcribe_audio_gemini(mime_type=mime, data=bytes(raw))
    except Exception as exc:
        logger.warning("live transcription failed chat=%s msg=%s: %s", chat_id, tg_msg_id, exc)
        return None


async def enrich_voice_messages(ctx: RetrievedContext) -> None:
    """Mutates ctx.messages in-place: replaces [voice] placeholders with transcription text."""
    candidates = [m for m in ctx.messages if _needs_transcription(m)]
    if not candidates:
        return

    live_budget = _MAX_LIVE
    for msg in candidates:
        chat_id = msg.get("chat_id")
        tg_msg_id = msg.get("telegram_msg_id")
        if not chat_id or not tg_msg_id:
            continue

        transcription = await _lookup_chunk_transcription(int(chat_id), int(tg_msg_id))

        if not transcription and live_budget > 0:
            live_budget -= 1
            transcription = await _live_transcribe(int(chat_id), int(tg_msg_id))

        if transcription:
            msg["text"] = transcription
            msg["transcribed"] = True
