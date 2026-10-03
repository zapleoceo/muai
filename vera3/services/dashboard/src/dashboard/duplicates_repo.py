"""Данные страницы дублей одним вызовом: откуда берутся пары-кандидаты и чьи
досье нужны, чтобы показать «кто это». Маршрут больше не знает, из каких
запросов страница собрана; сам SQL живёт в `vera_shared.graph`."""
from __future__ import annotations

from dataclasses import dataclass, field

from vera_shared.graph.collisions import find_email_collisions
from vera_shared.graph.dedup import (
    find_alias_collisions,
    find_duplicates_by_name,
    get_entity_dossiers,
)
from vera_shared.graph.identity import list_pending_suggestions

NAME_GROUPS_SHOWN = 25
CANDIDATES_PER_GROUP = 6


@dataclass(frozen=True)
class DuplicatesData:
    collisions: list[dict] = field(default_factory=list)
    name_groups: list[dict] = field(default_factory=list)
    name_groups_total: int = 0
    suggestions: list[dict] = field(default_factory=list)
    dossiers: dict[int, dict] = field(default_factory=dict)
    email_pairs: int = 0


def _trim(group: dict) -> dict:
    return {**group, "candidates": group["candidates"][:CANDIDATES_PER_GROUP],
            "hidden": max(0, group["size"] - CANDIDATES_PER_GROUP)}


def _entity_ids(collisions: list[dict], suggestions: list[dict],
                name_groups: list[dict]) -> set[int]:
    ids = {c["id"] for g in collisions for c in g["candidates"]}
    for sg in suggestions:
        ids.update((sg["entity_a"], sg["entity_b"]))
    ids.update(c["id"] for g in name_groups for c in g["candidates"])
    return ids


async def load_duplicates() -> DuplicatesData:
    collisions = await find_alias_collisions(min_group=2)
    groups = await find_duplicates_by_name(min_group=2)
    shown = [_trim(g) for g in groups[:NAME_GROUPS_SHOWN]]
    suggestions = await list_pending_suggestions()
    # Досье всех кандидатов одним батч-вызовом: по запросу на сущность пул
    # соединений кончился бы на двухстах кандидатах.
    dossiers = await get_entity_dossiers(sorted(_entity_ids(collisions, suggestions, shown)))
    email_pairs = sum(1 for g in await find_email_collisions(min_group=2) if len(g["candidates"]) == 2)
    return DuplicatesData(collisions, shown, len(groups), suggestions, dossiers, email_pairs)
