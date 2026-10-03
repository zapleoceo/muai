"""Перенос дочерних строк при слиянии: алиасы, членства, связи, аватары,
узлы идентичности, предложения. Каждая функция меняет строки через ORM и
пишет след в `Recorder` — по следу `unmerge` всё вернёт."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.models_graph import (
    EntityAliasRow,
    EntityAvatarRow,
    IdentityNodeRow,
    MembershipRow,
    MergeSuggestionRow,
    RelationshipRow,
)
from vera_shared.graph.merge_report import Recorder


def split_alias_conflicts(
    keep_keys: set[tuple[str, str]], incoming: Iterable[EntityAliasRow],
) -> tuple[list[EntityAliasRow], list[EntityAliasRow]]:
    """(переезжают, конфликтуют). Конфликт — такой же (source, identifier) уже
    у keep: в БД он уникален глобально, но защита дешевле, чем падение слияния."""
    moves: list[EntityAliasRow] = []
    conflicts: list[EntityAliasRow] = []
    seen = set(keep_keys)
    for alias in incoming:
        key = (alias.source, alias.identifier)
        (conflicts if key in seen else moves).append(alias)
        seen.add(key)
    return moves, conflicts


async def merge_aliases(s: AsyncSession, rec: Recorder, keep_id: int,
                        drop_ids: list[int]) -> None:
    keep_rows = (await s.execute(select(EntityAliasRow).where(
        EntityAliasRow.entity_id == keep_id))).scalars().all()
    incoming = (await s.execute(select(EntityAliasRow).where(
        EntityAliasRow.entity_id.in_(drop_ids)).order_by(EntityAliasRow.id))
    ).scalars().all()
    by_key = {(a.source, a.identifier): a for a in keep_rows}
    moves, conflicts = split_alias_conflicts(set(by_key), incoming)
    for alias in conflicts:
        survivor = by_key.get((alias.source, alias.identifier))
        if survivor is not None and alias.confidence > survivor.confidence:
            rec.before_update(survivor, ["confidence"])
            survivor.confidence = alias.confidence
        rec.deleted(alias)
        await s.delete(alias)
    for alias in moves:
        rec.moved(alias, "entity_id", alias.entity_id)
        alias.entity_id = keep_id
    await s.flush()


def _remapper(keep_id: int, drops: set[int]) -> Callable[[int], int]:
    def remap(entity_id: int) -> int:
        return keep_id if entity_id in drops else entity_id
    return remap


_SEEN_FIELDS = ("is_current", "first_seen_at", "last_seen_at")
_REL_FIELDS = (*_SEEN_FIELDS, "confidence", "fact", "derived_from_event_id")

Edge = MembershipRow | RelationshipRow


def _widen(survivor: Edge, other: Edge) -> None:
    survivor.is_current = bool(survivor.is_current or other.is_current)
    survivor.first_seen_at = min(survivor.first_seen_at, other.first_seen_at)
    survivor.last_seen_at = max(survivor.last_seen_at, other.last_seen_at)


def _absorb_membership(survivor: MembershipRow, other: MembershipRow) -> None:
    _widen(survivor, other)


def _absorb_relationship(survivor: RelationshipRow, other: RelationshipRow) -> None:
    survivor.confidence = max(survivor.confidence, other.confidence)
    survivor.fact = survivor.fact or other.fact
    survivor.derived_from_event_id = (
        survivor.derived_from_event_id or other.derived_from_event_id)
    _widen(survivor, other)


async def _fold_edges(
    s: AsyncSession, rec: Recorder, keep_id: int, drop_ids: list[int], *,
    model: type[Edge], ends: tuple[str, str], tracked: tuple[str, ...],
    key: Callable[[Edge, int, int], tuple], absorb: Callable[[Any, Any], None],
) -> None:
    """Общий обход рёбер (членства, связи): drop → keep, дубль склеивается в
    победителя, петля удаляется. `ends` — две колонки-конца ребра."""
    remap = _remapper(keep_id, set(drop_ids))
    first, second = (getattr(model, c) for c in ends)
    rows = (await s.execute(select(model).where(or_(
        first.in_(drop_ids), second.in_(drop_ids))).order_by(model.id))).scalars().all()
    existing = {key(r, getattr(r, ends[0]), getattr(r, ends[1])): r
                for r in (await s.execute(select(model).where(or_(
                    first == keep_id, second == keep_id)))).scalars()}
    for row in rows:
        new = [remap(getattr(row, c)) for c in ends]
        survivor = existing.get(key(row, *new))
        if new[0] == new[1] or (survivor is not None and survivor is not row):
            if survivor is not None and new[0] != new[1]:
                rec.before_update(survivor, tracked)
                absorb(survivor, row)
            rec.deleted(row)
            await s.delete(row)
            continue
        for column, value in zip(ends, new, strict=True):
            if value != getattr(row, column):
                rec.moved(row, column, getattr(row, column))
                setattr(row, column, value)
        existing[key(row, *new)] = row
    await s.flush()


async def merge_memberships(s: AsyncSession, rec: Recorder, keep_id: int,
                            drop_ids: list[int]) -> None:
    await _fold_edges(
        s, rec, keep_id, drop_ids, model=MembershipRow,
        ends=("parent_entity_id", "child_entity_id"), tracked=_SEEN_FIELDS,
        key=lambda r, a, b: (a, b, r.source), absorb=_absorb_membership)


async def merge_relationships(s: AsyncSession, rec: Recorder, keep_id: int,
                              drop_ids: list[int]) -> None:
    await _fold_edges(
        s, rec, keep_id, drop_ids, model=RelationshipRow,
        ends=("subject_entity_id", "object_entity_id"), tracked=_REL_FIELDS,
        key=lambda r, a, b: (a, r.predicate, b), absorb=_absorb_relationship)


def _has_photo(av: EntityAvatarRow | None) -> bool:
    return av is not None and av.image is not None and not av.missing


async def merge_avatars(s: AsyncSession, rec: Recorder, keep_id: int,
                        drop_ids: list[int]) -> None:
    keep_av = await s.get(EntityAvatarRow, keep_id)
    drop_avs = list((await s.execute(select(EntityAvatarRow).where(
        EntityAvatarRow.entity_id.in_(drop_ids)).order_by(EntityAvatarRow.entity_id))
    ).scalars())
    winner = None
    if not _has_photo(keep_av):
        winner = next((a for a in drop_avs if _has_photo(a)),
                      drop_avs[0] if drop_avs and keep_av is None else None)
    if winner is not None and keep_av is not None:
        rec.deleted(keep_av)
        await s.delete(keep_av)
        await s.flush()
    for av in drop_avs:
        if av is winner:
            old = av.entity_id
            av.entity_id = keep_id
            rec.moved(av, "entity_id", old)
        else:
            rec.deleted(av)
            await s.delete(av)
    await s.flush()


async def move_identity_nodes(s: AsyncSession, rec: Recorder, keep_id: int,
                              drop_ids: list[int]) -> None:
    for node in (await s.execute(select(IdentityNodeRow).where(
            IdentityNodeRow.listener_entity_id.in_(drop_ids)))).scalars():
        rec.before_update(node, ["updated_at"])  # onupdate=now() сдвинет метку
        rec.moved(node, "listener_entity_id", node.listener_entity_id)
        node.listener_entity_id = keep_id
    await s.flush()


async def merge_suggestions(s: AsyncSession, rec: Recorder, keep_id: int,
                            drop_ids: list[int]) -> None:
    drops = set(drop_ids)
    rows = (await s.execute(select(MergeSuggestionRow).where(or_(
        MergeSuggestionRow.entity_a.in_(drop_ids),
        MergeSuggestionRow.entity_b.in_(drop_ids))).order_by(MergeSuggestionRow.id))
    ).scalars().all()
    pairs = {(m.entity_a, m.entity_b) for m in (await s.execute(select(
        MergeSuggestionRow).where(or_(MergeSuggestionRow.entity_a == keep_id,
                                      MergeSuggestionRow.entity_b == keep_id)))).scalars()}
    for row in rows:
        a, b = (keep_id if x in drops else x for x in (row.entity_a, row.entity_b))
        pair = (min(a, b), max(a, b))
        if a == b or pair in pairs:
            rec.deleted(row)
            await s.delete(row)
            continue
        pairs.add(pair)
        if row.entity_a != pair[0]:
            rec.moved(row, "entity_a", row.entity_a)
        if row.entity_b != pair[1]:
            rec.moved(row, "entity_b", row.entity_b)
        row.entity_a, row.entity_b = pair
    await s.flush()
