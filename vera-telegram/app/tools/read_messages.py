import asyncio
import io
import logging
from datetime import datetime, timedelta, timezone

from telethon.errors import FloodWaitError
from telethon.tl.types import (
    MessageMediaPhoto, MessageMediaDocument, MessageMediaWebPage,
)

from app.userbot.client import get_client

log = logging.getLogger(__name__)

_OCR_INSTRUCTION = (
    "Это вложение из Telegram. Извлеки весь читаемый текст. "
    "Если это таблица или баланс — сохрани структуру (имена колонок, "
    "значения построчно). Если рисунок без значимого текста — короткое "
    "описание (1 строка). Не комментируй, только содержимое."
)
_OCR_MAX_PER_THREAD = 6
_OCR_MAX_BYTES = 8 * 1024 * 1024
_OCR_SEM = asyncio.Semaphore(1)


async def _ocr_media(msg) -> str | None:
    """Download photo/image-doc bytes and OCR via Gemini. Returns text or
    None. Bounded by size + global semaphore so we don't burst the quota."""
    try:
        from vera_shared.media.multimodal import media_to_text
        from vera_shared.tokens.pool import TokensExhausted
    except Exception:
        return None

    media = msg.media
    if media is None:
        return None
    mime = None
    if isinstance(media, MessageMediaPhoto):
        mime = "image/jpeg"
    elif isinstance(media, MessageMediaDocument):
        doc = getattr(media, "document", None)
        mime = getattr(doc, "mime_type", None) or ""
        if not mime.startswith("image/"):
            return None
    else:
        return None

    try:
        buf = io.BytesIO()
        async with _OCR_SEM:
            await msg.download_media(file=buf)
            data = buf.getvalue()
            if not data or len(data) > _OCR_MAX_BYTES:
                return None
            return await media_to_text(mime, data, _OCR_INSTRUCTION)
    except TokensExhausted as exc:
        log.warning("OCR tokens exhausted: %s", exc)
        return None
    except Exception as exc:
        log.warning("OCR failed: %s", exc)
        return None


def _sender_name(msg) -> str:
    sender = getattr(msg, "_sender", None) or getattr(msg, "sender", None)
    if sender is None:
        return "unknown"
    title = getattr(sender, "title", None)
    if title:
        return title
    fn = getattr(sender, "first_name", None) or ""
    ln = getattr(sender, "last_name", None) or ""
    return " ".join(filter(None, [fn, ln])) or str(getattr(sender, "id", ""))


def _entity_name(entity) -> str:
    title = getattr(entity, "title", None)
    if title:
        return title
    fn = getattr(entity, "first_name", None) or ""
    ln = getattr(entity, "last_name", None) or ""
    return " ".join(filter(None, [fn, ln])) or str(getattr(entity, "id", ""))


async def _resolve_peer_by_id_or_name(peer: str):
    """Resolve a peer string to a Telethon entity.

    For numeric ids — try as User (positive), basic Chat (negative),
    and Channel/Supergroup (-100 prefix). DialogFilters store raw
    entity ids (positive), so a channel_id 1234567890 must be looked
    up as -1001234567890. Without this, get_entity(positive) defaults
    to PeerUser → silently returns wrong entity / 0 messages.
    """
    from telethon.tl.types import PeerChannel, PeerChat, PeerUser
    client = get_client()
    if peer.lstrip("-").isdigit():
        pid = int(peer)
        # Try as channel/supergroup first if id is large (channel_ids
        # are usually > 10^9; user ids span the whole range too, so we
        # try multiple).
        candidates = []
        if pid > 0:
            candidates = [PeerChannel(pid), PeerChat(pid), PeerUser(pid)]
        else:
            candidates = [int(peer)]  # already-marked id
        last_exc = None
        for c in candidates:
            try:
                return await client.get_entity(c)
            except Exception as exc:
                last_exc = exc
                continue
        raise LookupError(f"cannot resolve peer {peer}: {last_exc}")
    # Search ONLY user's own dialogs (no global Telegram search)
    q = peer.lower()
    try:
        async for d in client.iter_dialogs(limit=500):
            name = _entity_name(d.entity).lower()
            if q in name:
                return d.entity
    except FloodWaitError as exc:
        raise LookupError(f"telegram flood wait {exc.seconds}s") from exc
    raise LookupError(
        f"chat '{peer}' not found in your dialogs — use telegram_search_dialogs to find the chat_id"
    )


async def read_messages(peer: str, limit: int = 50, offset_days: int = 1,
                         ocr_images: bool = True) -> dict:
    if not peer:
        raise LookupError("peer empty — call telegram_search_dialogs first")
    client = get_client()
    entity = await _resolve_peer_by_id_or_name(peer)
    cutoff = datetime.now(timezone.utc) - timedelta(days=offset_days)

    raw_messages = []
    async for msg in client.iter_messages(entity, limit=limit):
        if msg.date and msg.date < cutoff:
            break
        await msg.get_sender()
        raw_messages.append(msg)

    ocr_budget = _OCR_MAX_PER_THREAD if ocr_images else 0
    messages: list[dict] = []
    for msg in raw_messages:
        text = msg.text or ""
        has_image = False
        if msg.media is not None:
            if isinstance(msg.media, MessageMediaPhoto):
                has_image = True
            elif isinstance(msg.media, MessageMediaDocument):
                mime = getattr(getattr(msg.media, "document", None),
                               "mime_type", "") or ""
                has_image = mime.startswith("image/")
        ocr_text = None
        if has_image and ocr_budget > 0:
            ocr_text = await _ocr_media(msg)
            ocr_budget -= 1
            if ocr_text:
                text = (text + "\n\n" if text else "") + f"[OCR]:\n{ocr_text}"
            else:
                text = (text + "\n" if text else "") + "[image: не удалось распознать]"
        elif has_image:
            text = (text + "\n" if text else "") + "[image: OCR пропущен]"
        messages.append({
            "id": msg.id,
            "date": msg.date.isoformat() if msg.date else None,
            "text": text,
            "from": _sender_name(msg),
            "out": msg.out,
            "has_image": has_image,
            "has_ocr": bool(ocr_text),
        })

    return {
        "chat_id": entity.id,
        "chat_name": _entity_name(entity),
        "messages_count": len(messages),
        "messages": messages,
    }
