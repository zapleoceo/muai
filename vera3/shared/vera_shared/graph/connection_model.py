"""Связь как пара: роли и вес по числу и виду контактов — чистые функции, без базы.

Модель владельца: между двумя сущностями ОДНА связь с параметрами и уликами, а не
россыпь независимых предикатов. Роль («начальник», «клиент», «работает с»)
берётся из записанных `relationships`, но её вес складывается из трёх вещей:
сколько записей её подтверждают, как плотно пара общается и правил ли роль
владелец руками. Работа вместе (`coworker_of`) может быть и ВЫВЕДЕНА из
рабочих контактов без единой фразы про неё.

Пороги подобраны по прод-замеру 04.10.2026 (число дней, когда пара «в контакте»):
владелец и коллеги с общим рабочим доменом — медиана 10 дней, p25 2, p75 24, p90
47; владелец и остальные собеседники — медиана 2, p75 4, p90 16; пары вне
владельца — p90 5–9. Отсюда: 4 дня — верхняя четверть обычных собеседников,
10 — рубеж «постоянный контакт».
"""
from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from vera_shared.graph.pair_stats import PairStats, ordered
from vera_shared.graph.rel_canon import SYMMETRIC, canonical_edge

#: Вклад одной записи уверенности 1.0 в вес роли; две записи — 1-(1-0.5)².
CLAIM_SUPPORT = 0.5
#: Роль, заданную руками (MCP, дашборд: у связи нет события-источника), — максимум.
MANUAL_WEIGHT = 1.0
#: Масштаб насыщения взаимодействия: interaction = 1-exp(-дней/8) — 4 дня 0.39,
#: 8 дней 0.63, 10 дней 0.71, 30 дней 0.98.
DAYS_SCALE = 8.0
#: Общая небольшая группа — слабая улика: полдня контакта за группу, не больше четырёх.
GROUP_DAY_EQUIV = 0.5
GROUP_DAYS_CAP = 4
#: Со взаимодействия выше этого пара «устоявшаяся» (≈8 дней контакта): её роли судит
#: модель связи, а не одна фраза. Попадает ~18% личных контактов владельца.
ESTABLISHED_INTERACTION = 0.6
#: Верхняя граница выведенной роли «работает с»: выведенное слабее записанного вручную.
INFER_MAX = 0.8
#: Сколько рабочих дней нужно для вывода: с общим доменом / Slack хватает 3, по одним
#: рабочим чатам — 6 (в чате легко оказаться рядом без совместной работы).
INFER_MIN_DAYS_IDENT = 3
INFER_MIN_DAYS_CHAT = 6
#: Вторая роль показывается как «также упоминалось», если её вес не ниже порога и не
#: ниже доли от главной; остальные скрыты (данные остаются).
ALSO_MIN_WEIGHT = 0.35
ALSO_RATIO = 0.6
INFERRED_PREDICATE = "coworker_of"
#: При равном весе главной становится роль, стоящая раньше: она точнее.
ROLE_PRIORITY = ("spouse_of", "parent_of", "boss_of", "co_founder_of", "client_of",
                 "vendor_of", "friend_of", "coworker_of", "works_at", "lives_in")

RoleKey = tuple[str, int | None]


@dataclass(frozen=True)
class Claim:
    """Одна запись `relationships` как улика роли."""
    subject_id: int
    predicate: str
    object_id: int
    confidence: float
    manual: bool = False
    fact: str | None = None
    seen_at: datetime | None = None
    rel_id: int | None = None


@dataclass(frozen=True)
class Role:
    predicate: str
    subject_id: int | None      # кто «над»; у симметричных ролей None
    weight: float
    support: int                # сколько записей подтверждают (0 — роль выведена)
    manual: bool
    inferred: bool
    confidence: float
    fact: str | None
    rel_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class Connection:
    a: int
    b: int
    roles: tuple[Role, ...]     # по убыванию веса
    interaction: float
    stats: PairStats
    shared_work: bool

    @property
    def main(self) -> Role:
        return self.roles[0]

    @property
    def weight(self) -> float:
        return self.main.weight

    @property
    def also(self) -> tuple[Role, ...]:
        floor = max(ALSO_MIN_WEIGHT, ALSO_RATIO * self.weight)
        return tuple(r for r in self.roles[1:] if r.weight >= floor)

    @property
    def hidden(self) -> int:
        return len(self.roles) - 1 - len(self.also)


def interaction_strength(stats: PairStats) -> float:
    days = stats.active_days + GROUP_DAY_EQUIV * min(stats.shared_groups, GROUP_DAYS_CAP)
    return 1.0 - math.exp(-days / DAYS_SCALE)


def is_established(stats: PairStats) -> bool:
    return interaction_strength(stats) >= ESTABLISHED_INTERACTION


def inferred_work_weight(stats: PairStats, shared_work: bool) -> float:
    """Вес выведенного «работает с»; 0 — оснований нет."""
    days = stats.active_days if shared_work else stats.work_co_days
    if days < (INFER_MIN_DAYS_IDENT if shared_work else INFER_MIN_DAYS_CHAT):
        return 0.0
    return INFER_MAX * (1.0 - math.exp(-days / DAYS_SCALE))


def could_infer_work(stats: PairStats) -> bool:
    """Хватает ли дней для вывода «работает с» при каком-нибудь рабочем признаке."""
    return (inferred_work_weight(stats, True) > 0 or inferred_work_weight(stats, False) > 0)


def _role_key(claim: Claim) -> RoleKey:
    s, p, _ = canonical_edge(claim.subject_id, claim.predicate, claim.object_id)
    return p, None if p in SYMMETRIC else s


def _asserted_role(key: RoleKey, claims: list[Claim], interaction: float) -> Role:
    latest = max(claims, key=lambda c: (c.seen_at or datetime.min, len(c.fact or "")))
    manual = any(c.manual for c in claims)
    base = 1.0 - math.prod(1.0 - CLAIM_SUPPORT * c.confidence for c in claims)
    weight = MANUAL_WEIGHT if manual else min(1.0, base * (1.0 + interaction))
    return Role(key[0], key[1], weight, len(claims), manual, False,
                max(c.confidence for c in claims), latest.fact,
                tuple(sorted(c.rel_id for c in claims if c.rel_id is not None)))


def _priority(role: Role) -> tuple[float, int]:
    order = (ROLE_PRIORITY.index(role.predicate) if role.predicate in ROLE_PRIORITY
             else len(ROLE_PRIORITY))
    return -role.weight, order


def build_connection(a: int, b: int, claims: Iterable[Claim], stats: PairStats,
                     shared_work: bool = False) -> Connection | None:
    """Связь пары из записей и статистики; None — у пары нет ни одной роли."""
    interaction = interaction_strength(stats)
    grouped: dict[RoleKey, list[Claim]] = {}
    for claim in claims:
        grouped.setdefault(_role_key(claim), []).append(claim)
    roles = {key: _asserted_role(key, group, interaction) for key, group in grouped.items()}
    inferred = inferred_work_weight(stats, shared_work)
    work_key: RoleKey = (INFERRED_PREDICATE, None)
    if inferred and work_key in roles:
        old = roles[work_key]
        roles[work_key] = replace(old, weight=1.0 - (1.0 - old.weight) * (1.0 - inferred),
                                  inferred=True)
    elif inferred:
        roles[work_key] = Role(INFERRED_PREDICATE, None, inferred, 0, False, True, 0.0, None)
    if not roles:
        return None
    ranked = tuple(sorted(roles.values(), key=_priority))
    low, high = ordered(a, b)
    return Connection(low, high, ranked, interaction, stats, shared_work)


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
