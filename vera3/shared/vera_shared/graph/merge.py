"""Слияние сущностей графа — одной транзакцией и с возможностью отката.

Отличие от `dedup.merge_entities` (кнопка на /entities/duplicates): слить можно
сразу несколько сущностей в одну, а возвращаемый `MergeReport` достаточен для
`unmerge.unmerge` — удалённые сущности вернутся с прежними id.

События на сущности по id не ссылаются: `events` хранит `entity_hints` как
идентификаторы (email, @handle) и `metadata.sender_id` как telegram-id, а
`relationships.derived_from_event_id` указывает на событие, не на сущность.
Поэтому переносить за пределами графа нечего, и этот модуль трогает ровно
шесть таблиц: алиасы, членства, связи, аватары, узлы идентичности, предложения.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.engine import get_session
from vera_shared.db.models_graph import EntityRow
from vera_shared.graph import merge_children as children
from vera_shared.graph.merge_codec import row_dict
from vera_shared.graph.merge_report import MergeReport, Recorder
from vera_shared.timeutil import utc_naive_now


class MergeError(ValueError):
    """Слияние невозможно: нет сущности, пустой список или keep среди drop."""


def union_attributes(keep: dict[str, Any], drops: list[EntityRow]) -> dict[str, Any]:
    """Значения keep приоритетнее; чужие дозаполняют пустое, расхождения
    остаются в `merged_from` — иначе второе мнение источника пропало бы молча."""
    combined = dict(keep)
    history = list(combined.get("merged_from") or [])
    for drop in drops:
        theirs = dict(drop.attributes or {})
        theirs.pop("merged_from", None)
        differing = {k: v for k, v in theirs.items()
                     if k in keep and keep[k] != v and v not in (None, "")}
        for key, value in theirs.items():
            if key not in combined or combined[key] in (None, ""):
                combined[key] = value
        history.append({"id": drop.id, "name": drop.name, "type": drop.type,
                        "differing": differing})
    combined["merged_from"] = history
    return combined


async def _load(s: AsyncSession, keep_id: int, drop_ids: list[int]) -> tuple[EntityRow, list[EntityRow]]:
    keep = await s.get(EntityRow, keep_id)
    if keep is None:
        raise MergeError(f"keep {keep_id} не найден")
    drops = []
    for drop_id in drop_ids:
        drop = await s.get(EntityRow, drop_id)
        if drop is None:
            raise MergeError(f"drop {drop_id} не найден")
        drops.append(drop)
    return keep, drops


async def _merge(s: AsyncSession, keep_id: int, drop_ids: list[int],
                 reason: str) -> MergeReport:
    keep, drops = await _load(s, keep_id, drop_ids)
    report = MergeReport(keep_id=keep_id, drop_ids=list(drop_ids), reason=reason,
                         merged_at=utc_naive_now().isoformat(),
                         keep_before=row_dict(keep),
                         dropped=[row_dict(d) for d in drops])
    rec = Recorder(report)
    for step in (children.merge_aliases, children.merge_memberships,
                 children.merge_relationships, children.merge_avatars,
                 children.move_identity_nodes, children.merge_suggestions):
        await step(s, rec, keep_id, drop_ids)

    keep.attributes = union_attributes(dict(keep.attributes or {}), drops)
    keep.first_seen_at = min([keep.first_seen_at, *(d.first_seen_at for d in drops)])
    keep.last_seen_at = max([keep.last_seen_at, *(d.last_seen_at for d in drops)])
    for drop in drops:
        await s.delete(drop)
    await s.flush()
    return report


async def merge_entities(keep_id: int, drop_ids: list[int], reason: str, *,
                         session: AsyncSession | None = None) -> MergeReport:
    """Влить `drop_ids` в `keep_id`. Свою транзакцию открывает, только если
    не передана `session` (тогда коммит — забота вызывающего)."""
    drops = list(dict.fromkeys(drop_ids))
    if not drops:
        raise MergeError("drop_ids пуст")
    if keep_id in drops:
        raise MergeError(f"keep {keep_id} не может быть среди drop")
    if session is not None:
        return await _merge(session, keep_id, drops, reason)
    async with get_session() as s:
        return await _merge(s, keep_id, drops, reason)
