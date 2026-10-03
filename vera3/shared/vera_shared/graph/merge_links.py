"""Слияние сущностей и данные модуля связей (миграции 042–044).

Решения владельца и агентов не должны пропадать при слиянии:

- `entity_nicknames` — прозвища переезжают к победителю (токен у победителя уже есть — дубль
  удаляется, у победителя остаётся его строка);
- `voice_speaker_map` — карта голосов: то же;
- `event_entities`: ручные связи (`manual`) переносятся поштучно с записью в отчёт — `unmerge`
  вернёт их; ПРОИЗВОДНЫЕ связи переводятся на победителя одним UPDATE без поштучного следа
  (их десятки тысяч, отчёт слияния лежит в журнале целиком) — после слияния или отката
  точность возвращает пересчёт индекса (`backfill_event_links.py --reset`).
"""
from __future__ import annotations

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.models_links import EntityNicknameRow, EventEntityRow, VoiceSpeakerMapRow
from vera_shared.graph.merge_report import Recorder
from vera_shared.links.model import MANUAL

_KEY = ("event_id", "role", "token")


def _key(row: EventEntityRow) -> tuple:
    return tuple(getattr(row, c) for c in _KEY)


async def _merge_derived(s: AsyncSession, keep_id: int, drop_id: int) -> None:
    """Производные связи drop → keep; у победителя такой ключ уже есть — строка drop лишняя."""
    keep = EventEntityRow.__table__.alias("k")
    exists = select(keep.c.event_id).where(
        keep.c.event_id == EventEntityRow.event_id, keep.c.entity_id == keep_id,
        keep.c.role == EventEntityRow.role, keep.c.token == EventEntityRow.token).exists()
    await s.execute(update(EventEntityRow).where(
        EventEntityRow.entity_id == drop_id, EventEntityRow.source_of_link != MANUAL,
        ~exists).values(entity_id=keep_id))
    await s.execute(delete(EventEntityRow).where(
        EventEntityRow.entity_id == drop_id, EventEntityRow.source_of_link != MANUAL))


async def merge_event_entities(s: AsyncSession, rec: Recorder, keep_id: int,
                               drop_ids: list[int]) -> None:
    for drop_id in drop_ids:
        manual = (await s.execute(select(EventEntityRow).where(
            EventEntityRow.entity_id == drop_id, EventEntityRow.source_of_link == MANUAL))
        ).scalars().all()
        existing = {_key(r): r for r in (await s.execute(select(EventEntityRow).where(
            EventEntityRow.entity_id == keep_id,
            EventEntityRow.event_id.in_([r.event_id for r in manual])))).scalars()} if manual else {}
        for row in manual:
            rival = existing.get(_key(row))
            rec.deleted(row)
            fields = {c.key: getattr(row, c.key) for c in row.__table__.columns}
            await s.delete(row)
            if rival is not None and rival.source_of_link == MANUAL:
                continue                              # у победителя уже есть решение — оно главнее
            if rival is not None:                     # ручная связь важнее производной
                rec.deleted(rival)
                await s.delete(rival)
            await s.flush()
            s.add(EventEntityRow(**{**fields, "entity_id": keep_id}))
            rec.created(EventEntityRow.__tablename__, {
                "event_id": row.event_id, "entity_id": keep_id, "role": row.role,
                "token": row.token})
        await s.flush()
        await _merge_derived(s, keep_id, drop_id)
    await s.flush()


async def merge_nicknames(s: AsyncSession, rec: Recorder, keep_id: int,
                          drop_ids: list[int]) -> None:
    taken = {r.token for r in (await s.execute(select(EntityNicknameRow).where(
        EntityNicknameRow.entity_id == keep_id))).scalars()}
    rows = (await s.execute(select(EntityNicknameRow).where(
        EntityNicknameRow.entity_id.in_(drop_ids)).order_by(EntityNicknameRow.id))).scalars().all()
    for row in rows:
        if row.token in taken:
            rec.deleted(row)
            await s.delete(row)
            continue
        taken.add(row.token)
        rec.moved(row, "entity_id", row.entity_id)
        row.entity_id = keep_id
    await s.flush()


async def merge_voice_map(s: AsyncSession, rec: Recorder, keep_id: int,
                          drop_ids: list[int]) -> None:
    """Карта голосов: ключ строки — (kind, key), сущность — обычная колонка; переезд — UPDATE
    колонки `entity_id` (без конфликтов, ключ от сущности не зависит)."""
    for row in (await s.execute(select(VoiceSpeakerMapRow).where(
            VoiceSpeakerMapRow.entity_id.in_(drop_ids)))).scalars():
        rec.moved_by_keys(row, {"kind": row.kind, "key": row.key}, "entity_id", row.entity_id)
        row.entity_id = keep_id
    await s.flush()
