"""Роли по истории переписки в модели связи: изолированная надстройка над `build_connection`.

`build_connection` (роли из `relationships` + общение) не меняется: готовая `Connection`
дополняется выведенными ролями ПОСЛЕ сборки. Правила:

- вес = `HISTORY_MAX` × уверенность модели (≤ 0.85): ниже записанного вручную (1.0);
- с записанной ролью складывается как независимая улика (1 − (1−w₁)(1−w₂));
- ручная правка перекрывает: ручная роль не меняется, а роль, противоречащая ручной иерархии
  (и «A над B» при ручном «B над A»), не показывается;
- с записанной неручной противоположной стороной иерархии побеждает та, у кого вес выше;
- отвергнутая владельцем роль (`connection_suppressions`, предикат пары) не показывается;
- конкретная роль начальника / соучредителя / родителя вытесняет безликое выведенное «работает с»;
  нейтральное «общение без ясной роли» уступает любой роли;
- в карточке такая роль помечена «выведено из переписки», с обоснованием и цитатами.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.exc import DBAPIError

from vera_shared.db.engine import get_session
from vera_shared.graph.connection_model import (
    HIERARCHY,
    INFERRED_PREDICATE,
    NEUTRAL_PREDICATE,
    ROLE_PRIORITY,
    Connection,
    Role,
    RoleKey,
    interaction_strength,
    role_payload,
)
from vera_shared.graph.pair_roles_parse import MIN_CONFIDENCE
from vera_shared.graph.pair_roles_store import StoredRole
from vera_shared.graph.pair_roles_types import A_TO_B, BOTH
from vera_shared.graph.pair_stats import PairStats, ordered

log = logging.getLogger(__name__)

#: Потолок веса выведенной по истории роли: ниже ручной правки (`MANUAL_WEIGHT` = 1.0).
HISTORY_MAX = 0.85
HISTORY_LABEL = "выведено из переписки"
#: Эти роли вытесняют безликое выведенное «работает с»: «начальник» уже сказал больше.
SPECIFIC_WORK_ROLES = frozenset({"boss_of", "co_founder_of"})


@dataclass(frozen=True)
class HistoryRole(Role):
    """Роль, на которую повлияла история переписки: обоснование и цитаты доступны карточке."""
    rationale: str = ""
    quotes: tuple[str, ...] = ()
    model: str = ""
    computed_at: datetime | None = None


def _key(stored: StoredRole) -> RoleKey:
    low, high = ordered(stored.entity_a, stored.entity_b)
    if stored.direction == BOTH:
        return stored.predicate, None
    return stored.predicate, low if stored.direction == A_TO_B else high


def _rival(key: RoleKey, a: int, b: int) -> RoleKey | None:
    if key[0] not in HIERARCHY or key[1] is None:
        return None
    return key[0], b if key[1] == a else a


def _history_role(stored: StoredRole, key: RoleKey, base: Role | None) -> HistoryRole:
    weight = HISTORY_MAX * stored.confidence
    extra = {"rationale": stored.rationale, "quotes": stored.quotes, "model": stored.model,
             "computed_at": stored.computed_at}
    if base is None:
        return HistoryRole(key[0], key[1], weight, 0, False, True, stored.confidence,
                           stored.rationale or None, **extra)
    values = {f.name: getattr(base, f.name) for f in fields(Role)}
    values.update(weight=1.0 - (1.0 - base.weight) * (1.0 - weight), inferred=True,
                  confidence=max(base.confidence, stored.confidence),
                  fact=base.fact or stored.rationale or None)
    return HistoryRole(**values, **extra)


def _order(role: Role) -> tuple[float, int]:
    index = ROLE_PRIORITY.index(role.predicate) if role.predicate in ROLE_PRIORITY else len(ROLE_PRIORITY)
    return -role.weight, index


def apply_history(conn: Connection | None, a: int, b: int, stored: list[StoredRole],
                  muted: frozenset[str], stats: PairStats, shared_work: bool = False) -> Connection | None:
    """`conn` дополнена выведенными по истории ролями (см. правила модуля); без ролей — как была."""
    low, high = ordered(a, b)
    roles: dict[RoleKey, Role] = {(r.predicate, r.subject_id): r for r in (conn.roles if conn else ())
                                  if r.predicate != NEUTRAL_PREDICATE}
    added = False
    for item in sorted(stored, key=lambda s: -s.confidence):
        key = _key(item)
        if item.confidence < MIN_CONFIDENCE or item.predicate in muted:
            continue
        if (existing := roles.get(key)) is not None and existing.manual:
            continue
        rival_key = _rival(key, low, high)
        rival = roles.get(rival_key) if rival_key else None
        weight = HISTORY_MAX * item.confidence
        if rival is not None and (rival.manual or rival.weight > weight):
            continue
        if rival is not None and rival_key is not None:
            del roles[rival_key]
        roles[key] = _history_role(item, key, existing)
        added = True
    if not added:
        return conn
    specific = any(isinstance(r, HistoryRole) and r.predicate in SPECIFIC_WORK_ROLES
                   for r in roles.values())
    if specific:
        roles = {k: r for k, r in roles.items()
                 if not (k[0] == INFERRED_PREDICATE and r.inferred and r.support == 0)}
    hidden = conn.hidden_roles if conn else 0
    return Connection(low, high, tuple(sorted(roles.values(), key=_order)), hidden,
                      interaction_strength(stats), stats, shared_work)


def history_payload(role: Role, viewer_id: int | None = None) -> dict[str, Any]:
    """`role_payload` + для ролей по истории: источник, обоснование, цитаты."""
    payload = role_payload(role, viewer_id)
    if isinstance(role, HistoryRole):
        payload.update(source="history", source_label=HISTORY_LABEL, rationale=role.rationale,
                       quotes=list(role.quotes), model=role.model,
                       computed_at=role.computed_at.isoformat() if role.computed_at else None)
    return payload


async def suppressed_predicates(entity_id: int) -> dict[int, frozenset[str]]:
    """Отвергнутые владельцем роли пар сущности: собеседник → предикаты."""
    async with get_session() as s:
        try:
            rows = (await s.execute(text(
                "SELECT entity_a, entity_b, predicate FROM connection_suppressions "
                "WHERE entity_a = :e OR entity_b = :e"), {"e": entity_id})).all()
        except DBAPIError as e:
            log.warning("connection_suppressions не прочитана (миграция 041?): %s", e)
            return {}
    out: dict[int, set[str]] = {}
    for x, y, predicate in rows:
        out.setdefault(y if x == entity_id else x, set()).add(predicate)
    return {k: frozenset(v) for k, v in out.items()}


async def suppressed_predicates_within(ids: list[int]) -> dict[tuple[int, int], frozenset[str]]:
    unique = sorted(set(ids))
    if len(unique) < 2:
        return {}
    async with get_session() as s:
        try:
            rows = (await s.execute(
                text("SELECT entity_a, entity_b, predicate FROM connection_suppressions "
                     "WHERE entity_a IN :ids AND entity_b IN :ids")
                .bindparams(bindparam("ids", expanding=True)), {"ids": unique})).all()
        except DBAPIError as e:
            log.warning("connection_suppressions не прочитана (миграция 041?): %s", e)
            return {}
    out: dict[tuple[int, int], set[str]] = {}
    for x, y, predicate in rows:
        out.setdefault((x, y), set()).add(predicate)
    return {k: frozenset(v) for k, v in out.items()}
