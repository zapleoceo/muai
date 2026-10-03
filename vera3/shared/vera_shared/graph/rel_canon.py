"""Каноническая форма связи — чистые функции, без базы.

Симметричная связь («A коллега B» и «B коллега A») хранится одной строкой с
концами (меньший id, больший id). Обратная пара (`reports_to` / `child_of`)
хранится как `boss_of` / `parent_of` с переставленными концами: активная форма
читается слева направо как «кто над кем», ею же пользуются подписи дашборда и
степень узла, и в графе больше нет двух способов сказать одно и то же.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

SYMMETRIC = frozenset({"coworker_of", "friend_of", "spouse_of"})
INVERSE = {"reports_to": "boss_of", "child_of": "parent_of"}
# «A над B» и «B над A» одновременно — противоречие, а не две правды.
ANTISYMMETRIC = frozenset({"boss_of", "parent_of"})

Triple = tuple[int, str, int]


def canonical_edge(subject_id: int, predicate: str, object_id: int) -> Triple:
    if predicate in INVERSE:
        return object_id, INVERSE[predicate], subject_id
    if predicate in SYMMETRIC and subject_id > object_id:
        return object_id, predicate, subject_id
    return subject_id, predicate, object_id


def equivalent_forms(subject_id: int, predicate: str, object_id: int) -> list[Triple]:
    """Все записи, которыми та же связь могла быть сохранена раньше (канон первым)."""
    canon = canonical_edge(subject_id, predicate, object_id)
    s, p, o = canon
    forms = [canon, (o, p, s)] if p in SYMMETRIC else [canon]
    forms += [(o, inverse, s) for inverse, base in INVERSE.items() if base == p]
    return forms


def rival_forms(subject_id: int, predicate: str, object_id: int) -> list[Triple]:
    """Записи, которые ПРОТИВОРЕЧАТ связи: «B над A», раз утверждается «A над B»."""
    s, p, o = canonical_edge(subject_id, predicate, object_id)
    if p not in ANTISYMMETRIC:
        return []
    return [(o, p, s)] + [(s, inv, o) for inv, base in INVERSE.items() if base == p]


def strength(edge: Mapping[str, Any]) -> tuple[bool, float, int]:
    """Чья связь весомее при конфликте: ручная (без события-источника) →
    уверенность → длина факта. Больше — сильнее."""
    return (edge.get("derived_from_event_id") is None,
            float(edge.get("confidence") or 0.0), len(edge.get("fact") or ""))
