"""«Возможно, тот же человек»: подсказка для сущностей из одного слова.

Сущность «Лиза» из реплики — это часто осколок настоящего человека («Лиза
Иванова» с username и почтой). Здесь НИЧЕГО не сливается: слияние разрушительно и
решается владельцем на `/entities/duplicates`. Карточка лишь показывает догадку
двумя путями: у осколка есть ожидающее `merge_suggestions`, либо среди устоявшихся
контактов смотрящего есть полный тёзка по первому имени, с которым общения больше.
"""
from __future__ import annotations

from sqlalchemy import bindparam, text

from vera_shared.db.engine import get_session
from vera_shared.graph.connection_data import Card, entity_cards
from vera_shared.graph.connection_model import Connection, is_established
from vera_shared.graph.identity import canonical_name_parts

MAX_HINTS = 3
REASON_SUGGESTION = "suggestion"
REASON_TWIN = "twin"


def _is_fragment(card: Card) -> bool:
    return card.type == "person" and len(card.name.split()) == 1


async def _pending_pairs(ids: list[int]) -> list[tuple[int, int]]:
    async with get_session() as s:
        rows = (await s.execute(
            text("SELECT entity_a, entity_b FROM merge_suggestions WHERE status = 'pending' "
                 "AND (entity_a IN :ids OR entity_b IN :ids)")
            .bindparams(bindparam("ids", expanding=True)), {"ids": ids})).all()
    return [(r[0], r[1]) for r in rows]


def _twins(fragment: int, cards: dict[int, Card],
           strength: dict[int, Connection]) -> list[int]:
    first = canonical_name_parts(cards[fragment].name)[0]
    own = strength[fragment].interaction
    return [i for i, conn in strength.items()
            if i != fragment and cards[i].type == "person" and not _is_fragment(cards[i])
            and canonical_name_parts(cards[i].name)[0] == first
            and is_established(conn.stats) and conn.interaction > own]


async def possible_same(viewer_id: int, conns: list[Connection],
                        cards: dict[int, Card]) -> dict[int, list[dict[str, object]]]:
    """id осколка → до трёх догадок `{id, name, reason}`; осколков без догадки нет в ответе."""
    strength = {(c.b if c.a == viewer_id else c.a): c for c in conns}
    fragments = [i for i in strength if i in cards and _is_fragment(cards[i])]
    if not fragments:
        return {}
    found: dict[int, dict[int, str]] = {i: {} for i in fragments}
    for twin_of in fragments:
        for twin in _twins(twin_of, cards, strength):
            found[twin_of][twin] = REASON_TWIN
    for a, b in await _pending_pairs(fragments):
        for mine, other in ((a, b), (b, a)):
            if mine in found:
                found[mine][other] = REASON_SUGGESTION
    names = {**cards, **await entity_cards(sorted({o for f in found.values() for o in f} - set(cards)))}
    return {i: [{"id": o, "name": names[o].name, "reason": reason}
                for o, reason in list(hints.items())[:MAX_HINTS] if o in names]
            for i, hints in found.items() if hints}
