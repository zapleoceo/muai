"""Which media gets LLM recognition (vision / whisper) — one policy, one place.

Set 2026-07-20 (Dima): the vision daily budget on the broker kept getting
exhausted by news-channel graphics and stickers — content with ~zero value
for searching Dima's own memory. So:

  - voice / audio → recognize (whisper; valuable everywhere, separate pool
    that isn't the bottleneck)
  - photo / image → recognize, EXCEPT in broadcast channels (news/memes,
    not personal correspondence) and EXCEPT noisy public groups (below)
  - sticker → never (tiny, emoji-like; the alt-text placeholder is enough)
  - everything else (video, video_note, document, …) → no recognition

Noisy-group denylist added 2026-07-29 (Dima: «это все в топку»): the
channel rule above only catches broadcast channels, but the biggest photo
sources were large public *groups* — expat communities and channel
discussion chats. They kept eating the vision budget that work and personal
chats need. Audited share: ~71% of the photo backlog.

Filtered media still lands in the brain — it keeps its `[photo]` placeholder
and metadata, only the LLM description is skipped.

Pure function so it's trivially unit-tested and shared by every ingest path.
"""
from __future__ import annotations

_RECOGNIZABLE_IMAGES = {"photo", "image"}

# Совпадение по НАЧАЛУ названия чата (регистр не важен), кроме явных
# подстрок. Держать в синхроне с NOISE_CHATS в scripts/vera-media-requeue.sh.
_NOISE_CHAT_PREFIXES = (
    "nexta live",          # + «NEXTA Live Chat» — чат-обсуждение канала
    "українці",            # экспат-группы: Вʼєтнам, Шрі-Ланка, Нячанг, курилка
    "хдніпро",
    "велигамность",
    "квизда",
    "ии - боты",
    "chatgpt",
    "канал лучкова",
)
_NOISE_CHAT_SUBSTRINGS = ("badcomedian",)


def is_noise_chat(chat_title: str | None) -> bool:
    """Шумный паблик/канал, фото из которого не распознаём."""
    if not chat_title:
        return False
    t = chat_title.strip().lower()
    return (t.startswith(_NOISE_CHAT_PREFIXES)
            or any(s in t for s in _NOISE_CHAT_SUBSTRINGS))


def should_recognize_media(
    media_kind: str | None,
    chat_kind: str | None,
    chat_title: str | None = None,
) -> bool:
    """True if this media should be sent for LLM recognition."""
    if media_kind in ("voice", "audio"):
        return True
    if media_kind in _RECOGNIZABLE_IMAGES:
        # Skip broadcast channels — their images are news/memes, not Dima's.
        if chat_kind == "channel":
            return False
        # …и шумные публичные группы (каналами не считаются, но такой же шум).
        return not is_noise_chat(chat_title)
    return False  # sticker, video, video_note, document, anything else
