"""Validate small inert documents without executing or extracting their contents."""
from __future__ import annotations

import json

from vera_shared.office.intake_input import DOCUMENT_MIMES, InvalidInput

MAX_TEXT_BYTES = 64 * 1024


def verify_document(content: bytes, mime: str) -> str:
    if mime not in DOCUMENT_MIMES or not 1 <= len(content) <= MAX_TEXT_BYTES:
        raise InvalidInput("document must be UTF-8 TXT/CSV/JSON, at most 65536 bytes")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise InvalidInput("document must be valid UTF-8") from None
    if any(ord(c) < 32 and c not in "\t\n\r" for c in text):
        raise InvalidInput("document contains binary control characters")
    if mime == "application/json":
        try:
            json.loads(text)
        except ValueError:
            raise InvalidInput("document is not valid JSON") from None
    return text
