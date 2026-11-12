"""Типы модели связи: улика `Claim`, роль `Role`, связь пары `Connection`."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from vera_shared.graph.connection_rules import (
    ALSO_MIN_WEIGHT,
    ALSO_RATIO,
    HIERARCHY,
    MIN_REPEAT_SUPPORT,
)
from vera_shared.graph.pair_stats import PairStats

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
        return tuple(r for r in self.roles[1:] if r.weight >= floor and is_corroborated(r)
                     and not (no_contact and r.predicate in HIERARCHY))

    @property
    def hidden(self) -> int:
        return self.hidden_roles + len(self.roles) - 1 - len(self.also)


def is_corroborated(role: Role) -> bool:
    return role.manual or role.inferred or role.support >= MIN_REPEAT_SUPPORT

