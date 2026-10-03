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
#: Роли, которым рабочее общение — прямая улика. Иерархии тут нет: рабочий контекст
#: доказывает «работает с», а не «кто начальник» («принял приглашение» → вес 0.95
#: у босса из одного сообщения, прод-QA 04.10).
WORK_ROLES = frozenset({"coworker_of", "works_at"})
#: Личные и коммерческие роли: одна фраза — слишком шаткое основание (прод-замер
#: 04.10: «супруга» и «поставщики» из одного упоминания перебивали 400 дней общих чатов).
NEEDS_REPEAT = frozenset({"spouse_of", "parent_of", "client_of", "vendor_of", "boss_of"})
MIN_REPEAT_SUPPORT = 2
#: Пара с общением, но без показываемой роли, не пропадает: «общение без ясной роли».
NEUTRAL_PREDICATE = "contact"
NEUTRAL_MAX_WEIGHT = 0.3
#: Сколько дней контакта достаточно, чтобы пара без записанных ролей показывалась как
#: «общение без ясной роли»: рубеж «постоянный контакт» — p90 обычных собеседников
#: владельца (16) и медиана его коллег (10); ≥10 дней у ~15% личных контактов, то есть
#: список короткий, но 108 дней и 615 личных сообщений уже не теряются.
CONTACT_MIN_DAYS = 10
#: Более конкретная роль, показываемая главной, поглощает менее конкретные: начальник
#: подразумевает «работает с», родитель и супруг — «дружит с». Поглощённая роль не
#: идёт в «также», её подтверждения прибавляются к показанным.
SPECIFIC_OVER = {"boss_of": frozenset({"coworker_of"}), "parent_of": frozenset({"friend_of"}),
                 "spouse_of": frozenset({"friend_of"})}
#: Иерархии, где обе стороны одновременно — противоречие.
HIERARCHY = frozenset({"boss_of", "parent_of"})
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
    event_id: int | None = None   # событие-источник: независимость улик считается по нему


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
    seen_at: datetime | None = None


@dataclass(frozen=True)
class Connection:
    a: int
    b: int
    roles: tuple[Role, ...]     # показываемые, по убыванию веса
    hidden_roles: int           # сколько ролей скрыто правилами показа
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
        no_contact = self.stats.active_days == 0
        return tuple(r for r in self.roles[1:] if r.weight >= floor and _corroborated(r)
                     and not (no_contact and r.predicate in HIERARCHY))

    @property
    def hidden(self) -> int:
        return self.hidden_roles + len(self.roles) - 1 - len(self.also)


def _corroborated(role: Role) -> bool:
    return role.manual or role.inferred or role.support >= MIN_REPEAT_SUPPORT


def _qualifies(role: Role) -> bool:
    return role.predicate not in NEEDS_REPEAT or _corroborated(role)


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
    (записей нет и общения меньше `CONTACT_MIN_DAYS`, либо роли скрыты и общения нет). `suppress_inferred` — владелец отверг выведенное
    «работает с»: общение не считается уликой, записанные роли остаются."""
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
