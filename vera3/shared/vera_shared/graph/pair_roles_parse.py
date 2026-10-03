"""Разбор ответа модели по паре: направление, проверка цитат по пакету, отсев шуток.

Роль принимается, только если: предикат из канонического набора, не помечена «только шутка
или ирония», уверенность не ниже `MIN_CONFIDENCE`, и ХОТЯ БЫ ОДНА цитата — дословная подстрока
пакета (нормализация как у `rel_verify`: регистр, пунктуация, пробелы). Цитаты, которых в пакете
нет, отбрасываются поштучно; роль без единой настоящей цитаты — целиком. Обе стороны
иерархии одновременно невозможны: остаётся более уверенная.
"""
from __future__ import annotations

import json
import re
from typing import Any

from vera_shared.graph.pair_roles_types import (
    A_TO_B,
    B_TO_A,
    BOTH,
    PREDICATES,
    RoleFinding,
)
from vera_shared.graph.rel_verify import normalize

MIN_CONFIDENCE = 0.5
MAX_QUOTES = 3
MIN_QUOTE_CHARS = 3
MAX_ROLES = 4
SYMMETRIC_PREDICATES = frozenset({"coworker_of", "co_founder_of", "friend_of", "spouse_of"})
HIERARCHY = ("boss_of", "parent_of")
_DIRECTION = {"A": A_TO_B, "B": B_TO_A, "both": BOTH}


class PairRolesFormatError(ValueError):
    """Ответ модели не по схеме."""


def valid_quotes(quotes: list[Any], corpus: str) -> tuple[str, ...]:
    """Дословные подстроки пакета; остальное отброшено."""
    haystack = normalize(corpus)
    out: list[str] = []
    for quote in quotes[:MAX_QUOTES + 2]:
        text = re.sub(r"\s+", " ", str(quote)).strip()
        needle = normalize(text)
        if len(needle) >= MIN_QUOTE_CHARS and needle in haystack and text not in out:
            out.append(text)
    return tuple(out[:MAX_QUOTES])


def _direction(predicate: str, subject: str) -> str | None:
    if predicate in SYMMETRIC_PREDICATES:
        return BOTH
    return _DIRECTION.get(subject) if subject != "both" else None


def _finding(item: Any, corpus: str) -> RoleFinding | None:
    if not isinstance(item, dict) or item.get("joke_or_irony_only") is True:
        return None
    predicate = str(item.get("predicate", ""))
    direction = _direction(predicate, str(item.get("subject", "")))
    if predicate not in PREDICATES or direction is None:
        return None
    try:
        confidence = max(0.0, min(1.0, float(item["confidence"])))
    except (KeyError, TypeError, ValueError):
        return None
    quotes = valid_quotes(list(item.get("quotes") or []), corpus)
    if confidence < MIN_CONFIDENCE or not quotes:
        return None
    return RoleFinding(predicate, direction, round(confidence, 2),
                       str(item.get("rationale", "")).strip()[:600], quotes)


def _drop_rival_hierarchy(findings: list[RoleFinding]) -> list[RoleFinding]:
    best: dict[str, RoleFinding] = {}
    for f in findings:
        if f.predicate in HIERARCHY and (f.predicate not in best
                                         or f.confidence > best[f.predicate].confidence):
            best[f.predicate] = f
    return [f for f in findings if f.predicate not in HIERARCHY or best[f.predicate] is f]


def parse_answer(raw: str, corpus: str) -> tuple[list[RoleFinding], str]:
    """(роли, резюме). Бросает `PairRolesFormatError`, если это не JSON по схеме."""
    try:
        data = json.loads(raw)
        roles, summary = data["roles"], str(data.get("relationship_summary", "")).strip()
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise PairRolesFormatError("answer is not valid pair_roles JSON") from e
    if not isinstance(roles, list):
        raise PairRolesFormatError("roles must be a list")
    found: dict[tuple[str, str], RoleFinding] = {}
    for item in roles[:MAX_ROLES * 2]:
        if (f := _finding(item, corpus)) is not None:
            key = (f.predicate, f.direction)
            if key not in found or found[key].confidence < f.confidence:
                found[key] = f
    kept = _drop_rival_hierarchy(sorted(found.values(), key=lambda f: -f.confidence))
    return kept[:MAX_ROLES], summary[:300]
