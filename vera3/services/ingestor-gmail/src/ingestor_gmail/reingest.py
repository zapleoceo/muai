"""Переразбор старых писем Jira по diff-правилам: чистое ядро без БД и сети.

Событие берётся в работу, только если отправитель — Jira (is_jira_sender), а
пересобранный тем же `_format_event`, что и у живого поллера, текст отличается
от сохранённого и несёт маркеры правок. Остальное не трогаем: повторный прогон
после применения даёт «identical» — идемпотентность без отдельного учёта.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ingestor_gmail.html_text import ADDED_OPEN, REMOVED_OPEN, is_jira_sender
from ingestor_gmail.poller import _format_event

CANDIDATE = "candidate"
IDENTICAL = "identical"
NO_MARKERS = "no_markers"

_FROM_LINE_RE = re.compile(r"^From:[ \t]*(.*)$", re.MULTILINE)


@dataclass(frozen=True)
class EventSnapshot:
    id: int
    source_event_id: str
    account: str | None
    content_text: str
    metadata: dict[str, Any] | None = field(default=None, compare=False)


@dataclass(frozen=True)
class Candidate:
    event: EventSnapshot
    new_text: str
    ms: float = 0.0


def sender_of(ev: EventSnapshot) -> str:
    stored = (ev.metadata or {}).get("from")
    if stored:
        return str(stored)
    found = _FROM_LINE_RE.search(ev.content_text.split("\n---\n", 1)[0])
    return found.group(1).strip() if found else ""


def is_jira_event(ev: EventSnapshot) -> bool:
    return bool(ev.account) and is_jira_sender(sender_of(ev))


def has_diff_markers(text: str) -> bool:
    return REMOVED_OPEN in text or ADDED_OPEN in text


def rebuild_content(ev: EventSnapshot, msg: dict) -> str:
    return _format_event(ev.account or "", msg)["content_text"]


def classify(ev: EventSnapshot, msg: dict) -> tuple[str, str]:
    """(вердикт, пересобранный текст)."""
    new = rebuild_content(ev, msg)
    if new == ev.content_text:
        return IDENTICAL, new
    if not has_diff_markers(new):
        return NO_MARKERS, new
    return CANDIDATE, new


def snippet_pair(old: str, new: str, width: int = 220) -> tuple[str, str]:
    """Окна «до/после» вокруг первого расхождения."""
    limit = min(len(old), len(new))
    pos = next((i for i in range(limit) if old[i] != new[i]), limit)
    start = max(0, pos - 40)
    return old[start:start + width], new[start:start + width]
