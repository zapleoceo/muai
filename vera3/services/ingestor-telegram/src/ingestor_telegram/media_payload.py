"""Медиа сообщения → base64 + mime для media-worker'а (vision/whisper).

Размер ограничен ДО скачивания и время тоже — см. media_download.py
(OOM 2026-07-19). Любой сбой — ответ `{"error": ...}`, а не 500: воркер
помечает медиа неудачным и идёт дальше.
"""
from __future__ import annotations

from base64 import b64encode
from typing import Any

from telethon import TelegramClient

from ingestor_telegram.media_download import (
    MediaDownloadTimeout,
    MediaTooLarge,
    download_capped,
)


def media_mime(msg: Any) -> str | None:
    if getattr(msg, "voice", None):
        return "audio/ogg"
    if getattr(msg, "audio", None):
        return getattr(msg.audio, "mime_type", "audio/mpeg")
    if getattr(msg, "photo", None):
        return "image/jpeg"
    if getattr(msg, "sticker", None):
        return getattr(msg.sticker, "mime_type", "image/webp")
    if getattr(msg, "document", None):
        return getattr(msg.document, "mime_type", "application/octet-stream")
    return None


async def media_payload(client: TelegramClient, chat_id: int, msg_id: int) -> dict[str, Any]:
    try:
        msg = await client.get_messages(chat_id, ids=msg_id)
        if msg is None:
            return {"error": "message not found"}
        if not getattr(msg, "media", None):
            return {"error": "no media on this message"}
        try:
            data = await download_capped(msg)
        except (MediaTooLarge, MediaDownloadTimeout) as e:
            return {"error": str(e)}
        if data is None:
            return {"error": "download returned None (deleted?)"}
        return {"b64": b64encode(data).decode("ascii"), "mime": media_mime(msg),
                "size": len(data)}
    except Exception as e:
        # Ответ воркеру, а не 500: см. докстринг модуля.
        return {"error": f"{type(e).__name__}: {e}"}
