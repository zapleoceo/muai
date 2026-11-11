"""Слияние двух сущностей: всё с merged переезжает на keeper, merged удаляется."""
from __future__ import annotations

from vera_shared.db.engine import get_session
from vera_shared.graph.dedup_merge_store import (
    delete_entity_row,
    load_attribute_pair,
    move_aliases,
    move_memberships,
    move_relationships,
    write_attributes,
)
from vera_shared.graph.dedup_store import _as_dict


async def merge_entities(keeper_id: int, merged_id: int) -> dict:
    """Move all aliases / memberships / relationships from merged → keeper, delete merged.

    Idempotent on uniqueness constraints (alias source+identifier UNIQUE,
    membership unique, relationships unique by subject+predicate+object).
    Conflicting rows during reassign are dropped — they already point at the
    correct keeper somehow.

    Returns counts of what moved + the deleted entity ID.
    """
    if keeper_id == merged_id:
        return {"error": "keeper_id == merged_id, refusing"}

    async with get_session() as s:
        # ATTRIBUTES: слить, а не потерять. Строка merged удаляется целиком, и
        # до 2026-08-26 её attributes уходили вместе с ней — при слиянии
        # Igor/Olga/Ruslan так пропали телефон и должность из профиля Slack
        # (email выжил только потому, что его пишет ещё и почтовый ингестор).
        # Значения keeper'а приоритетнее: он выбран как более полная сторона,
        # а merged лишь дозаполняет пустые места.
        keeper_attrs, merged_attrs = await load_attribute_pair(s, keeper_id, merged_id)
        combined = {**_as_dict(merged_attrs), **_as_dict(keeper_attrs)}
        attrs_gained = len(combined) - len(_as_dict(keeper_attrs))
        if attrs_gained:
            await write_attributes(s, keeper_id, combined)
        aliases_moved, aliases_dropped = await move_aliases(s, keeper_id, merged_id)
        mems_moved = await move_memberships(s, keeper_id, merged_id)
        await move_relationships(s, keeper_id, merged_id)
        await delete_entity_row(s, merged_id)

    return {
        "keeper_id": keeper_id,
        "merged_id": merged_id,
        "aliases_moved": aliases_moved,
        "aliases_dropped": aliases_dropped,
        "memberships_moved": mems_moved,
        "attributes_gained": attrs_gained,
    }
