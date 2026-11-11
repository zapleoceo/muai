"""Маскирование свободного текста в выдаче комнаты. Идентификаторы, статусы,
fencing_token, аренда, время и ссылки не трогаются: хранится и сверяется сырое,
по ним работают идемпотентность и координатор."""
from __future__ import annotations

from typing import Any

from vera_shared.redact_view import redact_fields

_MESSAGE_TEXT = ("body",)
_TASK_TEXT = ("title", "note", "next_action", "last_progress_text", "waiting_reason")
_EVENT_TEXT = ("text",)
_QUESTION_TEXT = ("question",)
# Свободный текст внутри event.data: до/после правки метаданных, заметка, причина, передача.
_DATA_TEXT = ("next_action", "title", "note", "reason", "result", "question", "text")


def _refs(refs: Any) -> Any:
    if not isinstance(refs, list):
        return refs
    return [redact_fields(r, ("excerpt",)) if isinstance(r, dict) else r for r in refs]


def message_view(m: dict[str, Any], max_body_chars: int = 0) -> dict[str, Any]:
    out = redact_fields(m, _MESSAGE_TEXT)
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
    out = redact_fields(t, _TASK_TEXT)
    if "refs" in t:
        out["refs"] = _refs(t["refs"])
    if "answered_questions" in t:  # выдача claim: текст вопроса и ответов владельца
        out["answered_questions"] = questions_view(t["answered_questions"] or [])
    return out


def tasks_view(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [task_view(t) for t in items]


def _data(data: Any) -> Any:
    if not isinstance(data, dict):
        return data
    out = redact_fields(data, _DATA_TEXT)
    for side in ("before", "after"):
        if isinstance(data.get(side), dict):
            out[side] = redact_fields(data[side], _DATA_TEXT)
    if "refs" in data:
        out["refs"] = _refs(data["refs"])
    return out


def events_view(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**redact_fields(e, _EVENT_TEXT), "data": _data(e.get("data"))} if "data" in e
            else redact_fields(e, _EVENT_TEXT) for e in items]


def question_view(q: dict[str, Any]) -> dict[str, Any]:
    masked = redact_fields(q, _QUESTION_TEXT)
    if "answers" in q:
        masked["answers"] = [redact_fields(a, ("text",)) for a in q["answers"] or []]
    return masked


def questions_view(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [question_view(q) for q in items]
