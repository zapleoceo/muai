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
# Свободный текст внутри event.data: до/после правки метаданных, заметка, причина, передача.
_DATA_TEXT = ("next_action", "title", "note", "reason", "result", "question", "text")


def _mask(item: dict[str, Any], keys: Iterable[str]) -> dict[str, Any]:
    return {k: redact_view(v) if k in keys and isinstance(v, str) else v for k, v in item.items()}


def _refs(refs: Any) -> Any:
    if not isinstance(refs, list):
        return refs
    return [_mask(r, ("excerpt",)) if isinstance(r, dict) else r for r in refs]


def message_view(m: dict[str, Any], max_body_chars: int = 0) -> dict[str, Any]:
    out = _mask(m, _MESSAGE_TEXT)
    body = out["body"]
    truncated = max_body_chars > 0 and len(body) > max_body_chars
    return {**out, "body": body[:max_body_chars] if truncated else body,
            "truncated": truncated}


def messages_view(items: list[dict[str, Any]], max_body_chars: int = 0
                  ) -> list[dict[str, Any]]:
    return [message_view(m, max_body_chars) for m in items]


def task_view(t: dict[str, Any] | None) -> dict[str, Any] | None:
    if t is None:
        return None
    out = _mask(t, _TASK_TEXT)
    if "refs" in t:
        out["refs"] = _refs(t["refs"])
    return out


def tasks_view(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [task_view(t) for t in items]


def _data(data: Any) -> Any:
    if not isinstance(data, dict):
        return data
    out = _mask(data, _DATA_TEXT)
    for side in ("before", "after"):
        if isinstance(data.get(side), dict):
            out[side] = _mask(data[side], _DATA_TEXT)
    if "refs" in data:
        out["refs"] = _refs(data["refs"])
    return out


def events_view(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**_mask(e, _EVENT_TEXT), "data": _data(e.get("data"))} if "data" in e
            else _mask(e, _EVENT_TEXT) for e in items]


def question_view(q: dict[str, Any]) -> dict[str, Any]:
    masked = _mask(q, _QUESTION_TEXT)
    if "answers" in q:
        masked["answers"] = [_mask(a, ("text",)) for a in q["answers"] or []]
    return masked


def questions_view(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [question_view(q) for q in items]
