"""Текст события → человеческие поля.

`events.content_text` собран ингесторами как заголовок «Ключ: значение» и,
после строки `---`, тело сообщения. В списках и в источниках ответа заголовок
показывать сырым нельзя: «Author: … From: … To: …» не отвечает на вопрос «кто
и о чём». Здесь он разбирается один раз, а представления берут готовые поля.

События без заголовка (Claude, память агента, голос) приходят целиком как
тело — для них `headers` пуст.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from vera_shared.ingest.quotes import strip_quoted

HEADER_KEYS = frozenset({"Author", "From", "To", "Cc", "Subject", "Chat", "Where",
                         "Date", "Direction"})
SNIPPET_CHARS = 160
_HEADER_LINE = re.compile(r"^([A-Za-z]+):[ \t]*(.*)$")
_ROLE_TAG = re.compile(r"\s*\[[a-z_]+\]\s*$")
_ADDRESS = re.compile(r"^\s*\"?([^<\"]*?)\"?\s*<([^>]+)>\s*$")
_HANDLE = re.compile(r"^\s*(.*?)\s*\((@[\w.]+)\)\s*$")
_KIND_SUFFIX = re.compile(r"\s*\((?:channel|group|supergroup|private|user|bot)\)\s*$")
_REPLY_PREFIX = re.compile(r"^\s*((?:re|fwd?|отв)\s*:\s*)+", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedText:
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""


@dataclass(frozen=True)
class EventLine:
    who: str
    venue: str
    body: str
    subject: str = ""


def parse_content(text: str | None) -> ParsedText:
    headers: dict[str, str] = {}
    lines = (text or "").splitlines()
    pos = 0
    for pos, line in enumerate(lines):
        if line.strip() == "---":
            pos += 1
            break
        match = _HEADER_LINE.match(line)
        if match is None or match.group(1) not in HEADER_KEYS:
            break
        headers[match.group(1)] = match.group(2).strip()
    else:
        pos = len(lines)
    if not headers:
        return ParsedText({}, (text or "").strip())
    return ParsedText(headers, "\n".join(lines[pos:]).strip())


def clean_name(raw: str) -> str:
    """«Имя <mail>», «Имя (@ник)», «Автор [роль]» → «Имя»; без имени — адрес/ник."""
    value = _ROLE_TAG.sub("", raw).strip()
    if (m := _ADDRESS.match(value)) is not None:
        return m.group(1).strip() or m.group(2).strip()
    if (m := _HANDLE.match(value)) is not None:
        return m.group(1) or m.group(2)
    return value.strip('"')


def normalize_subject(subject: str) -> str:
    return _REPLY_PREFIX.sub("", subject).strip().casefold()


def _author(headers: Mapping[str, str], meta: Mapping[str, Any]) -> str:
    for key in ("From", "Author"):
        if headers.get(key) and (name := clean_name(headers[key])):
            return name
    return clean_name(str(meta.get("author_label") or ""))


def _venue(headers: Mapping[str, str], meta: Mapping[str, Any]) -> str:
    chat = meta.get("chat_title") or meta.get("channel_name")
    if chat:
        return str(chat)
    if headers.get("Chat"):
        return _KIND_SUFFIX.sub("", headers["Chat"])
    if headers.get("Where"):
        return headers["Where"]
    return str(meta.get("window_title") or "")


def describe(parsed: ParsedText, meta: Mapping[str, Any] | None = None) -> EventLine:
    """Кто написал, где, и что (одной строкой в списках решает представление)."""
    data = meta or {}
    subject = parsed.headers.get("Subject", "")
    venue = subject or _venue(parsed.headers, data)
    return EventLine(_author(parsed.headers, data), venue, parsed.body, subject)


def snippet_of(body: str, limit: int = SNIPPET_CHARS) -> str:
    """Одна строка из тела без истории ответа (общий помощник списков и источников)."""
    return one_line(strip_quoted(body), limit)


def one_line(text: str, limit: int = SNIPPET_CHARS) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit].rstrip() + "…"
