"""Правка и скрытие события — примитивы, работающие в сессии вызывающего.

Сессию открывает вызывающий, чтобы правка и запись в журнал (`mcp_audit`)
фиксировались одной транзакцией. Никакого DELETE: «убрать» событие = статус
`hidden` (`visibility`).

Правка текста возвращает событие в очередь триажа (`pending`): так же, как
`claude_session`, иначе поиск продолжит находить прежний текст по старому
embedding. У скрытого события очередь не трогаем — снятие скрытия вернёт
его в работу.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.models import EventRow
from vera_shared.events.visibility import (
    HIDDEN_STATUS,
    hide_values,
    unhide_values,
)


class EventNotFound(LookupError):
    def __init__(self, event_id: int) -> None:
        super().__init__(f"event {event_id} not found")


async def load_row(s: AsyncSession, event_id: int) -> EventRow:
    row = (await s.execute(
        select(EventRow).where(EventRow.id == event_id)
    )).scalar_one_or_none()
    if row is None:
        raise EventNotFound(event_id)
    return row


def snapshot(row: EventRow) -> dict[str, Any]:
    return {
        "content_text": row.content_text,
        "metadata": row.metadata_,
        "category": row.category,
        "triage_status": row.triage_status,
        "triage_error": row.triage_error,
        "triage_metadata": row.triage_metadata,
    }


def _requeue(row: EventRow) -> None:
    if row.triage_status == HIDDEN_STATUS:
        return
    row.triage_status = "pending"
    row.triage_error = None
    row.triage_started_at = None


def merge_metadata(old: dict[str, Any] | None,
                   patch: dict[str, Any]) -> dict[str, Any]:
    """Слияние по ключам верхнего уровня; значение None удаляет ключ."""
    out = dict(old or {})
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        else:
            out[key] = value
    return out


async def update_event(
    s: AsyncSession, event_id: int, *, content_text: str | None = None,
    metadata_patch: dict[str, Any] | None = None, category: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(до, после). Меняет только переданные поля."""
    row = await load_row(s, event_id)
    before = snapshot(row)
    if content_text is not None and content_text != row.content_text:
        row.content_text = content_text
        _requeue(row)
    if metadata_patch:
        row.metadata_ = merge_metadata(row.metadata_, metadata_patch)
    if category is not None:
        row.category = category
    await s.flush()
    return before, snapshot(row)


async def set_hidden(s: AsyncSession, event_id: int, *,
                     hidden: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    row = await load_row(s, event_id)
    before = snapshot(row)
    if hidden:
        row.triage_status, row.triage_metadata = hide_values(
            row.triage_status, row.triage_metadata)
    elif row.triage_status == HIDDEN_STATUS:
        row.triage_status, row.triage_metadata = unhide_values(row.triage_metadata)
    await s.flush()
    return before, snapshot(row)


async def restore_event(s: AsyncSession, event_id: int,
                        values: dict[str, Any]) -> dict[str, Any]:
    """Вернуть поля из снимка; изменённый текст — повод пере-эмбеддить."""
    row = await load_row(s, event_id)
    text_changed = values["content_text"] != row.content_text
    row.content_text = values["content_text"]
    row.metadata_ = values["metadata"]
    row.category = values["category"]
    row.triage_status = values["triage_status"]
    row.triage_error = values["triage_error"]
    row.triage_metadata = values["triage_metadata"]
    if text_changed:
        _requeue(row)
    await s.flush()
    return snapshot(row)
