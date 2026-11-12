"""Связь как пара: роли и вес по числу и виду контактов — чистые функции, без базы.

Модель владельца: между двумя сущностями ОДНА связь с параметрами и уликами, а не
россыпь независимых предикатов. Роль («начальник», «клиент», «работает с»)
берётся из записанных `relationships`; вес записанной роли — ТОЛЬКО от утверждений
(сколько записей, какая уверенность, правил ли руками владелец). Общение —
улика лишь для рабочих ролей (`WORK_ROLES`) и только в рабочем контексте (общий
рабочий домен / Slack / рабочие чаты): личная переписка говорит о близости, а не о
роли, и «супруга» из одной фразы не поднимет. Работа вместе (`coworker_of`) может
быть ВЫВЕДЕНА из рабочих контактов без единой фразы про неё.
Личные и коммерческие роли (`NEEDS_REPEAT`) без двух независимых записей или ручной
правки не показываются; «также» требует тех же двух записей; противоречивая
иерархия оставляет сторону с большей поддержкой.

Пороги и их прод-замер — `connection_rules`, типы `Claim`/`Role`/`Connection` —
`connection_types`; здесь сила общения, сборка связи и выдача роли. Все имена
реэкспортируются отсюда.
"""
from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime
from typing import Any

from vera_shared.graph.connection_rules import (
    ALSO_MIN_WEIGHT,
    ALSO_RATIO,
    CLAIM_SUPPORT,
    CONTACT_MIN_DAYS,
    DAYS_SCALE,
    ESTABLISHED_INTERACTION,
    GROUP_DAY_EQUIV,
    GROUP_DAYS_CAP,
    HIERARCHY,
    INFER_MAX,
    INFER_MIN_DAYS_CHAT,
    INFER_MIN_DAYS_IDENT,
    INFERRED_PREDICATE,
    MANUAL_WEIGHT,
    MIN_REPEAT_SUPPORT,
    NEEDS_REPEAT,
    NEUTRAL_MAX_WEIGHT,
    NEUTRAL_PREDICATE,
    ROLE_PRIORITY,
    SPECIFIC_OVER,
    WORK_ROLES,
)
from vera_shared.graph.connection_types import (
    Claim,
    Connection,
    Role,
    RoleKey,
    is_corroborated,
)
from vera_shared.graph.pair_stats import PairStats, ordered
from vera_shared.graph.rel_canon import SYMMETRIC, canonical_edge

__all__ = [
    "ALSO_MIN_WEIGHT", "ALSO_RATIO", "CLAIM_SUPPORT", "CONTACT_MIN_DAYS", "DAYS_SCALE",
    "ESTABLISHED_INTERACTION", "GROUP_DAYS_CAP", "GROUP_DAY_EQUIV", "HIERARCHY",
    "INFERRED_PREDICATE", "INFER_MAX", "INFER_MIN_DAYS_CHAT", "INFER_MIN_DAYS_IDENT",
    "MANUAL_WEIGHT", "MIN_REPEAT_SUPPORT", "NEEDS_REPEAT", "NEUTRAL_MAX_WEIGHT",
    "NEUTRAL_PREDICATE", "ROLE_PRIORITY", "SPECIFIC_OVER", "WORK_ROLES", "Claim",
    "Connection", "Role", "RoleKey", "build_connection", "could_infer_work",
    "inferred_work_weight", "interaction_strength", "is_established",
    "is_regular_contact", "role_payload", "work_strength",
]


def _qualifies(role: Role) -> bool:
    return role.predicate not in NEEDS_REPEAT or is_corroborated(role)


def interaction_strength(stats: PairStats) -> float:
    days = stats.active_days + GROUP_DAY_EQUIV * min(stats.shared_groups, GROUP_DAYS_CAP)
    return 1.0 - math.exp(-days / DAYS_SCALE)


def is_established(stats: PairStats) -> bool:
    return interaction_strength(stats) >= ESTABLISHED_INTERACTION


def _work_days(stats: PairStats, shared_work: bool) -> int:
    return stats.active_days if shared_work else stats.work_co_days


def work_strength(stats: PairStats, shared_work: bool) -> float:
    """Насыщение РАБОЧЕГО общения: общий домен / Slack — все дни контакта, иначе дни
    в рабочих чатах. Личная переписка без рабочего признака — 0."""
    return 1.0 - math.exp(-_work_days(stats, shared_work) / DAYS_SCALE)


def inferred_work_weight(stats: PairStats, shared_work: bool) -> float:
    """Вес выведенного «работает с»; 0 — оснований нет."""
    if _work_days(stats, shared_work) < (INFER_MIN_DAYS_IDENT if shared_work
                                         else INFER_MIN_DAYS_CHAT):
        return 0.0
    return INFER_MAX * work_strength(stats, shared_work)


def is_regular_contact(stats: PairStats) -> bool:
    return stats.active_days >= CONTACT_MIN_DAYS


def could_infer_work(stats: PairStats) -> bool:
    """Хватает ли дней для вывода «работает с» при каком-нибудь рабочем признаке."""
    return (inferred_work_weight(stats, True) > 0 or inferred_work_weight(stats, False) > 0)


def _role_key(claim: Claim) -> RoleKey:
    s, p, _ = canonical_edge(claim.subject_id, claim.predicate, claim.object_id)
    return p, None if p in SYMMETRIC else s


def _asserted_role(key: RoleKey, claims: list[Claim], work: float) -> Role:
    latest = max(claims, key=lambda c: (c.seen_at or datetime.min, len(c.fact or "")))
    manual = any(c.manual for c in claims)
    # Независимые улики — разные события-источники: одно сообщение, извлечённое
    # дважды, считается один раз (лучшая уверенность); ручные строки не в счёте.
    per_event: dict[object, float] = {}
    for c in claims:
        if not c.manual:
            source = c.event_id if c.event_id is not None else id(c)
            per_event[source] = max(per_event.get(source, 0.0), c.confidence)
    base = 1.0 - math.prod(1.0 - CLAIM_SUPPORT * conf for conf in per_event.values())
    boost = work if key[0] in WORK_ROLES else 0.0
    weight = MANUAL_WEIGHT if manual else min(1.0, base * (1.0 + boost))
    return Role(key[0], key[1], weight, len(per_event), manual, False,
                max(c.confidence for c in claims), latest.fact,
                tuple(sorted(c.rel_id for c in claims if c.rel_id is not None)),
                latest.seen_at)


def _priority(role: Role) -> tuple[bool, float, int]:
    order = (ROLE_PRIORITY.index(role.predicate) if role.predicate in ROLE_PRIORITY
             else len(ROLE_PRIORITY))
    return role.predicate not in SPECIFIC_OVER, -role.weight, order


def _survivor_rank(role: Role) -> tuple[object, ...]:
    """Какая сторона иерархии остаётся: ручная, поддержка, вес, свежесть, меньший id записи."""
    return (role.manual, role.support, role.weight, role.seen_at or datetime.min,
            -(min(role.rel_ids) if role.rel_ids else 0))


def _drop_contradictions(roles: dict[RoleKey, Role]) -> dict[RoleKey, Role]:
    """Обе стороны иерархии сразу — оставляем лучшую по `_survivor_rank`."""
    for predicate in HIERARCHY:
        sides = [k for k in roles if k[0] == predicate]
        best = max(sides, key=lambda k: _survivor_rank(roles[k]), default=None)
        for key in sides:
            if key != best:
                del roles[key]
    return roles


def _absorb(shown: dict[RoleKey, Role]) -> tuple[dict[RoleKey, Role], int]:
    """Показанная конкретная роль забирает менее конкретные: вес — больший из двух,
    подтверждения и id записей складываются. Возвращает роли и число поглощённых."""
    folded = 0
    for key in [k for k in shown if k[0] in SPECIFIC_OVER]:
        for other in [k for k in shown if k[0] in SPECIFIC_OVER[key[0]]]:
            small, big = shown.pop(other), shown[key]
            shown[key] = replace(big, weight=max(big.weight, small.weight),
                                 support=big.support + small.support,
                                 rel_ids=tuple(sorted({*big.rel_ids, *small.rel_ids})))
            folded += 1
    return shown, folded


def _neutral(stats: PairStats, interaction: float) -> Role | None:
    if stats.active_days <= 0:
        return None
    return Role(NEUTRAL_PREDICATE, None, NEUTRAL_MAX_WEIGHT * interaction, 0, False, False,
                0.0, None)


def build_connection(a: int, b: int, claims: Iterable[Claim], stats: PairStats,
                     shared_work: bool = False, suppress_inferred: bool = False) -> Connection | None:
    """Связь пары из записей и статистики; None — нет показываемой роли и не на что опереться
    (записей нет и общения меньше `CONTACT_MIN_DAYS`, либо роли скрыты и общения нет).
    `suppress_inferred` — владелец отверг выведенное «работает с»: общение не считается
    уликой, записанные роли остаются."""
    interaction = interaction_strength(stats)
    work = work_strength(stats, shared_work)
    grouped: dict[RoleKey, list[Claim]] = {}
    for claim in claims:
        grouped.setdefault(_role_key(claim), []).append(claim)
    roles = {key: _asserted_role(key, group, work) for key, group in grouped.items()}
    inferred = 0.0 if suppress_inferred else inferred_work_weight(stats, shared_work)
    work_key: RoleKey = (INFERRED_PREDICATE, None)
    if inferred and work_key in roles:
        old = roles[work_key]
        roles[work_key] = replace(old, weight=1.0 - (1.0 - old.weight) * (1.0 - inferred),
                                  inferred=True)
    elif inferred:
        roles[work_key] = Role(INFERRED_PREDICATE, None, inferred, 0, False, True, 0.0, None)
    qualified = {k: r for k, r in roles.items() if _qualifies(r)}
    kept, folded = _absorb(_drop_contradictions(qualified))
    shown = list(kept.values())
    hidden = len(roles) - len(shown) - folded
    if not shown:
        neutral = (_neutral(stats, interaction)
                   if roles or stats.active_days >= CONTACT_MIN_DAYS else None)
        if neutral is None:
            return None
        shown = [neutral]
    low, high = ordered(a, b)
    return Connection(low, high, tuple(sorted(shown, key=_priority)), hidden, interaction,
                      stats, shared_work)


def role_payload(role: Role, viewer_id: int | None = None) -> dict[str, Any]:
    """Роль словарём; `direction` — от лица смотрящего: out «он над другим»."""
    if role.subject_id is None:
        direction = "both"
    else:
        direction = "out" if role.subject_id == viewer_id else "in"
    return {"predicate": role.predicate, "direction": direction,
            "weight": round(role.weight, 2), "support": role.support,
            "confidence": round(role.confidence, 2), "manual": role.manual,
            "inferred": role.inferred, "fact": role.fact,
            **({"rel_ids": list(role.rel_ids)} if role.rel_ids else {})}
