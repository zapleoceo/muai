"""Слияние сущностей и отвергнутые пары (`connection_suppressions`, миграция 041).

Отметка «это неверно» принадлежит ПАРЕ людей: когда один из них вливается в
победителя, отметка переезжает к нему. Пара снова упорядочивается (a < b),
петля (оба конца стали победителем) и дубль уже имеющейся отметки удаляются —
`unmerge` вернёт всё по следу в `Recorder`."""
from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.models_graph import ConnectionSuppressionRow as Row
from vera_shared.graph.merge_report import Recorder


async def merge_suppressions(s: AsyncSession, rec: Recorder, keep_id: int,
                             drop_ids: list[int]) -> None:
    drops = set(drop_ids)
    rows = (await s.execute(select(Row).where(or_(
        Row.entity_a.in_(drop_ids), Row.entity_b.in_(drop_ids))).order_by(Row.id))
    ).scalars().all()
    taken = {(r.entity_a, r.entity_b, r.predicate) for r in (await s.execute(
        select(Row).where(or_(Row.entity_a == keep_id, Row.entity_b == keep_id)))).scalars()}
    for row in rows:
        a, b = (keep_id if x in drops else x for x in (row.entity_a, row.entity_b))
        key = (min(a, b), max(a, b), row.predicate)
        if a == b or key in taken:
            rec.deleted(row)
            await s.delete(row)
            continue
        taken.add(key)
        if row.entity_a != key[0]:
            rec.moved(row, "entity_a", row.entity_a)
        if row.entity_b != key[1]:
            rec.moved(row, "entity_b", row.entity_b)
        row.entity_a, row.entity_b = key[0], key[1]
    await s.flush()
