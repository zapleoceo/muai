"""vera_shared.media_policy.should_recognize_media — recognition gating."""
from __future__ import annotations

import pytest
from vera_shared.media_policy import should_recognize_media


@pytest.mark.parametrize("media_kind,chat_kind,expected", [
    # voice/audio → always (whisper, valuable everywhere)
    ("voice", "channel", True),
    ("voice", "private", True),
    ("audio", "group", True),
    # photos: yes in private/group, NO in broadcast channels
    ("photo", "private", True),
    ("photo", "group", True),
    ("photo", "channel", False),
    ("image", "channel", False),
    ("image", "group", True),
    # stickers: never (Dima 2026-07-20 — skip entirely)
    ("sticker", "private", False),
    ("sticker", "group", False),
    ("sticker", "channel", False),
    # non-recognizable kinds
    ("video", "private", False),
    ("video_note", "private", False),
    ("document", "private", False),
    (None, "private", False),
    # unknown chat_kind: photo still recognized (only 'channel' is excluded)
    ("photo", None, True),
])
def test_should_recognize_media(media_kind, chat_kind, expected):
    assert should_recognize_media(media_kind, chat_kind) is expected
