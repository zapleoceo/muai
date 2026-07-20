"""Which media gets LLM recognition (vision / whisper) — one policy, one place.

Set 2026-07-20 (Dima): the vision daily budget on the broker kept getting
exhausted by news-channel graphics and stickers — content with ~zero value
for searching Dima's own memory. So:

  - voice / audio → recognize (whisper; valuable everywhere, separate pool
    that isn't the bottleneck)
  - photo / image → recognize, EXCEPT in broadcast channels (news/memes,
    not personal correspondence)
  - sticker → never (tiny, emoji-like; the alt-text placeholder is enough)
  - everything else (video, video_note, document, …) → no recognition

Pure function so it's trivially unit-tested and shared by every ingest path.
"""
from __future__ import annotations

_RECOGNIZABLE_IMAGES = {"photo", "image"}


def should_recognize_media(media_kind: str | None, chat_kind: str | None) -> bool:
    """True if this media should be sent for LLM recognition."""
    if media_kind in ("voice", "audio"):
        return True
    if media_kind in _RECOGNIZABLE_IMAGES:
        # Skip broadcast channels — their images are news/memes, not Dima's.
        return chat_kind != "channel"
    return False  # sticker, video, video_note, document, anything else
