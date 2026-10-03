"""Связь как пара: что видно в карточке, на графе и в чистке.

READ-модель: роли берутся из текущих `relationships`, взаимодействия — из кэша
`pair_stats`, вес считает `connection_model`. Ничего здесь не пишется.

Скорость карточки (≤50 соседей): три индексных SELECT'а (`ix_rel_subject`,
`ix_rel_object`, PK `pair_stats`) плюс карточки и алиасы соседей по PK и
`ix_alias_entity` — десятки миллисекунд, события не трогаются вовсе.
"""
from __future__ import annotations

from typing import Any

from vera_shared.graph.connection_data import (
    Card,
    WorkIdent,
    claims_of,
    claims_within,
    entity_cards,
    shared_work,
    work_idents,
)
from vera_shared.graph.connection_hints import possible_same
from vera_shared.graph.connection_model import (
    INFERRED_PREDICATE,
    Claim,
    Connection,
    build_connection,
    could_infer_work,
    is_established,
    role_payload,
)
from vera_shared.graph.pair_stats import PairStats, ordered, partner_stats, stats_within

CONNECTIONS_LIMIT = 50


def _by_pair(claims: list[Claim]) -> dict[tuple[int, int], list[Claim]]:
    grouped: dict[tuple[int, int], list[Claim]] = {}
    for claim in claims:
        grouped.setdefault(ordered(claim.subject_id, claim.object_id), []).append(claim)
    return grouped


def _connection(a: int, b: int, claims: list[Claim], stats: PairStats | None,
                idents: dict[int, WorkIdent]) -> Connection | None:
    return build_connection(a, b, claims, stats or PairStats(),
                            shared_work(idents.get(a), idents.get(b)))


def _interaction_payload(conn: Connection) -> dict[str, Any]:
    return {**conn.stats.as_dict(), "strength": round(conn.interaction, 2),
            "established": is_established(conn.stats)}


def _card_payload(conn: Connection, viewer_id: int, card: Card) -> dict[str, Any]:
    other = conn.b if conn.a == viewer_id else conn.a
    return {"other_id": other, "other_name": card.name, "other_type": card.type,
            "weight": round(conn.weight, 2),
            "main": role_payload(conn.main, viewer_id),
            "also": [role_payload(r, viewer_id) for r in conn.also],
            "hidden": conn.hidden, "shared_work": conn.shared_work,
            "interaction": _interaction_payload(conn)}


async def _connections_of(entity_id: int) -> tuple[list[Connection], dict[int, Card]]:
    """Связи сущности со всеми собеседниками, самые весомые первыми, и их карточки."""
    by_other = {(b if a == entity_id else a): claims
                for (a, b), claims in _by_pair(await claims_of(entity_id)).items()}
    stats = await partner_stats(entity_id)
    candidates = set(by_other) | {p for p, st in stats.items() if could_infer_work(st)}
    if not candidates:
        return [], {}
    cards = await entity_cards([entity_id, *candidates])
    idents = await work_idents([entity_id, *candidates])
    conns = [c for other in candidates if other in cards
             if (c := _connection(entity_id, other, by_other.get(other, []),
                                  stats.get(other), idents)) is not None]
    conns.sort(key=lambda c: (-c.weight, -c.interaction))
    return conns, cards


async def entity_connections(entity_id: int, limit: int = CONNECTIONS_LIMIT) -> list[dict[str, Any]]:
    """Связи сущности по одной на собеседника, самые весомые первыми. Каждая:
    главная роль, «также упоминалось», число скрытых ролей, взаимодействия и
    (для одиночного имени) догадка «возможно тот же человек»."""
    conns, cards = await _connections_of(entity_id)
    hints = await possible_same(entity_id, conns, cards)
    out = []
    for conn in conns[:limit]:
        payload = _card_payload(conn, entity_id, cards[conn.b if conn.a == entity_id else conn.a])
        if hint := hints.get(payload["other_id"]):
            payload["possible_same"] = hint
        out.append(payload)
    return out


async def inferred_partner_ids(entity_id: int) -> list[int]:
    """Собеседники, связь с которыми держится ТОЛЬКО на общении (без записанных ролей):
    у них нет строки в `relationships`, и без этого списка граф их не нашёл бы."""
    conns, _ = await _connections_of(entity_id)
    return [(c.b if c.a == entity_id else c.a) for c in conns
            if all(r.support == 0 for r in c.roles)]


def edge_payload(conn: Connection) -> dict[str, Any]:
    """Ребро графа: одна на пару; направление — от «над» к «под», у симметричных a→b."""
    main = conn.main
    source = main.subject_id if main.subject_id is not None else conn.a
    target = conn.b if source == conn.a else conn.a
    return {"source": source, "target": target, "predicate": main.predicate,
            "weight": round(conn.weight, 2), "confidence": round(conn.weight, 2),
            "support": main.support, "inferred": main.inferred,
            "also": [r.predicate for r in conn.also]}


async def connections_among(ids: list[int], predicate: str | None = None) -> list[dict[str, Any]]:
    """Рёбра-пары между сущностями из `ids`. С `predicate` — только пары, у которых
    есть такая роль (выведенная «работает с» считается для `coworker_of`)."""
    if len(ids) < 2:
        return []
    grouped = _by_pair(await claims_within(ids, predicate))
    stats = await stats_within(ids)
    if predicate in (None, INFERRED_PREDICATE):
        inferable = {pair for pair, st in stats.items() if could_infer_work(st)}
    else:
        inferable = set()
    pairs = set(grouped) | inferable
    idents = await work_idents(sorted({i for pair in pairs for i in pair}))
    conns = (_connection(a, b, grouped.get((a, b), []), stats.get((a, b)), idents)
             for a, b in sorted(pairs))
    edges = [edge_payload(c) for c in conns if c is not None]
    return [e for e in edges if predicate is None or predicate == e["predicate"]
            or predicate in e["also"]]


async def established_pairs(pairs: list[tuple[int, int]]) -> set[tuple[int, int]]:
    """Какие из пар (упорядоченных) устоявшиеся по взаимодействию — их роли не гасятся
    по одной фразе (`rel_cleanup_verify`)."""
    wanted = {ordered(a, b) for a, b in pairs if a != b}
    stats = await stats_within(sorted({i for pair in wanted for i in pair}))
    return {pair for pair in wanted if pair in stats and is_established(stats[pair])}

