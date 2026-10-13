"""Маскирование свободного текста в выдаче комнаты. Идентификаторы, статусы,
fencing_token, аренда, время и ссылки не трогаются: хранится и сверяется сырое,
по ним работают идемпотентность и координатор."""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from vera_shared.redact_view import redact_view

_MESSAGE_TEXT = ("body",)
_TASK_TEXT = ("title", "note", "next_action", "last_progress_text", "waiting_reason")
_EVENT_TEXT = ("text",)
_QUESTION_TEXT = ("question",)


def _mask(item: dict[str, Any], keys: Iterable[str]) -> dict[str, Any]:
    return {k: redact_view(v) if k in keys and isinstance(v, str) else v for k, v in item.items()}


def messages_view(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_mask(m, _MESSAGE_TEXT) for m in items]


def _task(t: dict[str, Any]) -> dict[str, Any]:
    out = _mask(t, _TASK_TEXT)
    if isinstance(t.get("refs"), list):
        out["refs"] = [_mask(r, ("excerpt",)) if isinstance(r, dict) else r for r in t["refs"]]
    return out


def tasks_view(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_task(t) for t in items]


def events_view(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_mask(e, _EVENT_TEXT) for e in items]


def questions_view(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for q in items:
        masked = _mask(q, _QUESTION_TEXT)
        masked["answers"] = [_mask(a, ("text",)) for a in q.get("answers") or []]
        out.append(masked)
    return out
