"""Preview small inert UTF-8 documents; all other bytes remain opaque files."""
from __future__ import annotations

from vera_shared.office.intake_input import DOCUMENT_MIMES

MAX_TEXT_BYTES = 64 * 1024


def document_text(content: bytes, mime: str) -> str | None:
    if mime not in DOCUMENT_MIMES or not 1 <= len(content) <= MAX_TEXT_BYTES:
        return None
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if any(ord(c) < 32 and c not in "\t\n\r" for c in text):
        return None
    return text
