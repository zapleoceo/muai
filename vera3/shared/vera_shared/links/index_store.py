"""Хранилище индекса связей: курсоры потоков, выборка событий пачкой, запись связей.

Выборка берёт только нужные колонки (`load_only` по сути): расшифровки созвонов и
`content_extra` читаются отдельным запросом и только для событий-созвонов. Запись пачки —
одной транзакцией: связи событий пачки удаляются, КРОМЕ ручных (`manual` — решения владельца
и агента), и вставляются заново, пропуская ключи, занятые ручной связью.
"""
from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.engine import get_session
from vera_shared.db.models import EventRow
from vera_shared.db.models_links import EventEntityRow, LinkCursorRow
from vera_shared.events.visibility import HIDDEN_STATUS
from vera_shared.ingest.envelope import message_body
from vera_shared.links.context_data import EventView
from vera_shared.links.model import MANUAL, Link

FORWARD, BACKFILL = "forward", "backfill"
MAX_TEXT_CHARS = 4000
MAX_VOICE_CHARS = 60_000
#: Читаем с запасом на шапку ингестора, потом режем по `MAX_TEXT_CHARS`.
_HEAD_CHARS = MAX_TEXT_CHARS * 2


def view_of_row(row: EventRow) -> EventView:
    """Представление полностью загруженной строки (тесты, ручной пересчёт одного события)."""
    return EventView(row.id, row.source, message_body(row.content_text)[:MAX_TEXT_CHARS],
                     row.metadata_ or {}, row.content_extra if isinstance(row.content_extra, dict)
                     else None, row.transcript_text, row.occurred_at,
                     row.triage_status == HIDDEN_STATUS)


async def _cursor(name: str) -> int | None:
    async with get_session() as s:
        row = await s.get(LinkCursorRow, name)
    return row.event_id if row else None


async def set_cursor(name: str, event_id: int) -> None:
    async with get_session() as s:
        row = await s.get(LinkCursorRow, name)
        if row is None:
            s.add(LinkCursorRow(name=name, event_id=event_id))
        else:
            row.event_id = event_id


async def read_cursors() -> dict[str, int]:
    async with get_session() as s:
        return {r.name: r.event_id for r in (await s.execute(select(LinkCursorRow))).scalars()}


async def max_event_id() -> int:
    async with get_session() as s:
        return int((await s.execute(select(EventRow.id).order_by(EventRow.id.desc()).limit(1)))
                   .scalar_one_or_none() or 0)


async def reset_cursors() -> int:
    """Начать заново: forward — с конца, backfill — от конца вниз. → текущий max(id)."""
    top = await max_event_id()
    await set_cursor(FORWARD, top)
    await set_cursor(BACKFILL, top + 1)
    return top


_CORE = (EventRow.id, EventRow.source, func.substr(EventRow.content_text, 1, _HEAD_CHARS),
         EventRow.metadata_, EventRow.occurred_at, EventRow.triage_status)


async def load_views(event_ids: list[int]) -> list[EventView]:
    """Представления событий (по возрастанию id); тяжёлые поля — только у созвонов."""
    if not event_ids:
        return []
    async with get_session() as s:
        rows = (await s.execute(select(*_CORE).where(EventRow.id.in_(event_ids))
                                .order_by(EventRow.id))).all()
        voice = [r[0] for r in rows if r[1] == "voice"]
        heavy = {r[0]: (r[1], r[2]) for r in (await s.execute(
            select(EventRow.id, EventRow.content_extra,
                   func.substr(EventRow.transcript_text, 1, MAX_VOICE_CHARS))
            .where(EventRow.id.in_(voice)))).all()} if voice else {}
    return [_view(r, *heavy.get(r[0], (None, None))) for r in rows]


def _view(row: tuple, extra: object, transcript: str | None) -> EventView:
    return EventView(row[0], row[1], message_body(row[2])[:MAX_TEXT_CHARS], row[3] or {},
                     extra if isinstance(extra, dict) else None, transcript, row[4],
                     row[5] == HIDDEN_STATUS)


async def next_batch(name: str, batch: int) -> list[int]:
    """Id следующей пачки потока; курсор при первом обращении ставится на конец."""
    cursor = await _cursor(name)
    if cursor is None:
        cursor = await max_event_id() + (1 if name == BACKFILL else 0)
        await set_cursor(name, cursor)
    query = select(EventRow.id)
    query = (query.where(EventRow.id > cursor).order_by(EventRow.id).limit(batch)
             if name == FORWARD else
             query.where(EventRow.id < cursor).order_by(EventRow.id.desc()).limit(batch))
    async with get_session() as s:
        return sorted((await s.execute(query)).scalars())


async def _manual_keys(s: AsyncSession, event_ids: list[int]) -> set[tuple[int, int, str, str]]:
    rows = (await s.execute(select(
        EventEntityRow.event_id, EventEntityRow.entity_id, EventEntityRow.role,
        EventEntityRow.token).where(EventEntityRow.event_id.in_(event_ids),
                                    EventEntityRow.source_of_link == MANUAL))).all()
    return {tuple(r) for r in rows}


async def write_links(event_ids: list[int], links: list[Link]) -> int:
    """Заменить производные связи событий; ручные не трогаются. → число вставленных строк."""
    best: dict[tuple[int, int, str, str], Link] = {}
    for link in links:
        key = (link.event_id, link.entity_id, link.role, link.token)
        if key not in best or best[key].confidence < link.confidence:
            best[key] = link
    async with get_session() as s:
        await s.execute(delete(EventEntityRow).where(
            EventEntityRow.event_id.in_(event_ids), EventEntityRow.source_of_link != MANUAL))
        taken = await _manual_keys(s, event_ids)
        rows = [EventEntityRow(event_id=k[0], entity_id=k[1], role=k[2], token=k[3],
                               source_of_link=v.source, confidence=v.confidence, span=v.span,
                               scope_ok=v.scope_ok) for k, v in best.items() if k not in taken]
        s.add_all(rows)
    return len(rows)
