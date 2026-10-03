"""Очередь проверки дублей: настоящие предложения и точные совпадения идентификаторов.

Совпадения только по имени сюда не попадают (601 группа тёзок — почти всегда разные люди):
остальных людей находят поиском в карточке. Очередь строится в том же порядке при каждом
запросе, поэтому позиция `n` в адресе остаётся осмысленной; после решения пара уходит из
очереди и на её место встаёт следующая. SQL — в `vera_shared.graph`.
"""
from __future__ import annotations

from dataclasses import dataclass

from vera_shared.graph.collisions import find_email_collisions
from vera_shared.graph.dedup import find_alias_collisions, get_entity_dossiers
from vera_shared.graph.identity import list_pending_suggestions
from vera_shared.graph.suggestions import list_decided_pairs


@dataclass(frozen=True)
class QueueItem:
    a: int
    b: int
    reason: str
    kind: str                      # suggestion | username | email
    confidence: float | None = None
    suggestion_id: int | None = None
    verdict: str | None = None


def _pairs(groups: list[dict]) -> list[tuple[int, int]]:
    return [tuple(sorted(c["id"] for c in g["candidates"])) for g in groups
            if len(g["candidates"]) == 2]


async def load_queue() -> list[QueueItem]:
    suggestions = await list_pending_suggestions()
    items = [QueueItem(sg["entity_a"], sg["entity_b"], sg["reason"], "suggestion",
                       sg["confidence"], sg["id"], sg["verdict"]) for sg in suggestions]
    seen = {tuple(sorted((i.a, i.b))) for i in items} | await list_decided_pairs()
    for kind, groups, why in (
            ("email", await find_email_collisions(min_group=2), "Одинаковый рабочий email"),
            ("username", await find_alias_collisions(min_group=2), "Одинаковый @username")):
        for a, b in _pairs(groups):
            if (a, b) not in seen:
                seen.add((a, b))
                items.append(QueueItem(a, b, why, kind))
    return items


async def pair_dossiers(a: int, b: int) -> dict[int, dict]:
    return await get_entity_dossiers([a, b])
