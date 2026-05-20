import logging
from io import BytesIO
from typing import Awaitable, Callable

from aiogram import Bot
from aiogram.types import Audio, Document, Message, PhotoSize, Video, Voice

from app.media.gemini_multimodal import media_to_text

log = logging.getLogger(__name__)

ProgressCb = Callable[[str], Awaitable[None]]

_TRANSCRIBE_VOICE = (
    "Transcribe this voice message verbatim. Output ONLY the spoken text "
    "in the language used. Do not summarize or comment."
)
_DESCRIBE_IMAGE = (
    "Describe this image in Russian. If there is any visible text or numbers, "
    "transcribe them verbatim. Keep the description focused and informative."
)
_EXTRACT_DOC = (
    "Extract the readable content of this document. Return the actual text "
    "(in original language). Preserve structure (headings, lists) where useful. "
    "Do not summarize."
)


async def _download(bot: Bot, file_id: str) -> bytes:
    file = await bot.get_file(file_id)
    bio = BytesIO()
    await bot.download(file, destination=bio)
    return bio.getvalue()


async def extract_text(bot: Bot, message: Message, progress: ProgressCb) -> str | None:
    if message.text:
        return message.text

    if message.voice:
        return await _handle_voice(bot, message.voice, message.caption, progress)
    if message.audio:
        return await _handle_audio(bot, message.audio, message.caption, progress)
    if message.video_note:
        return await _handle_voice(bot, message.video_note, message.caption, progress)
    if message.video:
        return await _handle_video(bot, message.video, message.caption, progress)
    if message.photo:
        return await _handle_photo(bot, message.photo[-1], message.caption, progress)
    if message.document:
        return await _handle_document(bot, message.document, message.caption, progress)

    return None


async def _handle_voice(bot: Bot, voice, caption: str | None, progress: ProgressCb) -> str:
    await progress("🎙️ Распознаю голосовое...")
    data = await _download(bot, voice.file_id)
    mime = getattr(voice, "mime_type", None) or "audio/ogg"
    text = await media_to_text(mime, data, _TRANSCRIBE_VOICE)
    return _pack("Голосовое", text, caption)


async def _handle_audio(bot: Bot, audio: Audio, caption, progress) -> str:
    await progress("🎵 Распознаю аудио...")
    data = await _download(bot, audio.file_id)
    mime = audio.mime_type or "audio/mpeg"
    text = await media_to_text(mime, data, _TRANSCRIBE_VOICE)
    return _pack(f"Аудио ({audio.file_name or '?'})", text, caption)


async def _handle_photo(bot: Bot, photo: PhotoSize, caption, progress) -> str:
    await progress("🖼️ Анализирую картинку...")
    data = await _download(bot, photo.file_id)
    text = await media_to_text("image/jpeg", data, _DESCRIBE_IMAGE)
    return _pack("Картинка", text, caption)


async def _handle_video(bot: Bot, video: Video, caption, progress) -> str:
    await progress("🎬 Анализирую видео...")
    data = await _download(bot, video.file_id)
    mime = video.mime_type or "video/mp4"
    text = await media_to_text(mime, data, _DESCRIBE_IMAGE)
    return _pack("Видео", text, caption)


async def _handle_document(bot: Bot, doc: Document, caption, progress) -> str:
    fname = doc.file_name or "document"
    mime = doc.mime_type or "application/octet-stream"
    await progress(f"📄 Читаю файл {fname}...")
    data = await _download(bot, doc.file_id)

    if mime.startswith("text/") or mime in ("application/json", "application/xml"):
        try:
            text = data.decode("utf-8", errors="replace")[:16000]
        except Exception:
            text = "(не удалось декодировать файл)"
    else:
        text = await media_to_text(mime, data, _EXTRACT_DOC)
    return _pack(f"Файл {fname}", text, caption)


def _pack(kind: str, content: str, caption: str | None) -> str:
    parts = [f"[{kind}]"]
    if content:
        parts.append(content)
    if caption:
        parts.append(f"\nПодпись пользователя: {caption}")
    return "\n".join(parts)
