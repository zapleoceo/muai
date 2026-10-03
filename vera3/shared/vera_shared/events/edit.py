"""Правка и скрытие события — примитивы, работающие в сессии вызывающего.

Сессию открывает вызывающий, чтобы правка и запись в журнал (`mcp_audit`)
фиксировались одной транзакцией; строка события берётся `FOR UPDATE`, чтобы
параллельная правка или откат не затёрли друг друга.

Никакого DELETE: «убрать» событие = статус `hidden` (`visibility`).

Правка текста возвращает событие в очередь триажа (`pending`) только из
`done`/`error`, как это делает `claude_session`, иначе поиск продолжит
находить прежний текст по старому embedding. Событие, занятое воркером
(`processing`, `media_pending`), не правится и не скрывается: воркер
перезапишет результат, а прежним статусом мы бы сохранили чужой.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.models import EventRow
from vera_shared.events.visibility import (
    BUSY_STATUSES,
    HIDDEN_STATUS,
    hide_values,
    unhide_values,
)

REQUEUE_FROM = ("done", "error")
#: поле снимка → атрибут ORM
_ATTR = {"content_text": "content_text", "metadata": "metadata_",
         "category": "category", "triage_status": "triage_status",
         "triage_error": "triage_error", "triage_metadata": "triage_metadata"}


class EventNotFound(LookupError):
    def __init__(self, event_id: int) -> None:
        super().__init__(f"event {event_id} not found")


class EventBusy(ValueError):
    def __init__(self, event_id: int, status: str) -> None:
        super().__init__(
            f"event {event_id} is '{status}' (being processed); retry in a minute")


async def load_row(s: AsyncSession, event_id: int) -> EventRow:
    row = (await s.execute(
        select(EventRow).where(EventRow.id == event_id).with_for_update()
    )).scalar_one_or_none()
    if row is None:
        raise EventNotFound(event_id)
    return row


def _require_idle(row: EventRow) -> None:
    if row.triage_status in BUSY_STATUSES:
        raise EventBusy(row.id, row.triage_status)


def snapshot(row: EventRow) -> dict[str, Any]:
    return {key: getattr(row, attr) for key, attr in _ATTR.items()}


def _requeue(row: EventRow) -> None:
    if row.triage_status not in REQUEUE_FROM:
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
    _require_idle(row)
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
    if hidden:
        _require_idle(row)
    before = snapshot(row)
    if hidden:
        row.triage_status, row.triage_metadata = hide_values(
            row.triage_status, row.triage_metadata)
    elif row.triage_status == HIDDEN_STATUS:
        row.triage_status, row.triage_metadata = unhide_values(row.triage_metadata)
    await s.flush()
    return before, snapshot(row)


async def restore_fields(s: AsyncSession, event_id: int, values: dict[str, Any],
                         keys: tuple[str, ...]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Вернуть ТОЛЬКО перечисленные поля снимка (откат не трогает чужие правки).
    Вернувшийся текст — повод пере-эмбеддить. (до, после)."""
    row = await load_row(s, event_id)
    _require_idle(row)
    before = snapshot(row)
    text_changed = "content_text" in keys and values["content_text"] != row.content_text
    for key in keys:
        setattr(row, _ATTR[key], values[key])
    if text_changed:
        _requeue(row)
    await s.flush()
    return before, snapshot(row)
