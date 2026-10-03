"""Скрытые события: статус `hidden` исключает событие из поиска и выдач.

Ни один воркер не берёт `hidden` (триаж клеймит только `pending`, сторож —
только `processing`/`error`), поэтому статус стабилен, пока его не снимут.
Прежний статус лежит в `triage_metadata`, чтобы снятие скрытия вернуло
событие ровно туда, где оно было (например `media_pending`).
"""
from __future__ import annotations

from typing import Any

HIDDEN_STATUS = "hidden"
#: Фрагмент WHERE для сырого SQL по `events` без алиаса.
NOT_HIDDEN_SQL = f"triage_status <> '{HIDDEN_STATUS}'"
PREV_STATUS_KEY = "hidden_prev_status"
DEFAULT_RESTORE_STATUS = "done"


def hide_values(status: str, meta: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
    """(новый статус, новая triage_metadata) для скрытия события."""
    out = dict(meta or {})
    if status != HIDDEN_STATUS:
        out[PREV_STATUS_KEY] = status
    return HIDDEN_STATUS, out


def unhide_values(meta: dict[str, Any] | None) -> tuple[str, dict[str, Any] | None]:
    """(статус, triage_metadata) для снятия скрытия."""
    out = dict(meta or {})
    status = out.pop(PREV_STATUS_KEY, DEFAULT_RESTORE_STATUS)
    return status, (out or None)
