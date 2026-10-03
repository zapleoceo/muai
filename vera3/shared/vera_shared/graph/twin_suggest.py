"""Двойники при создании сущности: предложение владельцу, а не слияние.

Человек из Telegram и тот же человек из рабочей почты/Slack не делят ни одного
идентификатора — связать их можно только по имени, а имя тёзок совпадает.
Автослияние по имени ошиблось бы на однофамильцах (аудит 2026-09-26 нашёл их
среди рабочих групп), поэтому ингестор лишь кладёт пару в `merge_suggestions`:
владелец видит её на /entities/duplicates, а точные случаи разбирает
`scripts/merge_graph_duplicates.py`. Строка в БД дешева и обратима, ошибочное
слияние — нет.

То же для чатов: супергруппа без известного `migrated_from` и группа с тем же
именем — вероятная миграция, но два разных чата тоже зовутся одинаково.
"""
from __future__ import annotations

import logging
import time

from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models_graph import EntityAliasRow, EntityRow
from vera_shared.graph.dupe_keys import name_key, plain_name
from vera_shared.graph.suggestions import propose_merge

log = logging.getLogger(__name__)

WORK_DOMAIN = "@itstep.org"
_INDEX_TTL_S = 600.0

_checked: set[int] = set()
_index: dict[str, list[tuple[int, str]]] = {}
_index_at = 0.0


def forget() -> None:
    global _index_at
    _checked.clear()
    _index.clear()
    _index_at = 0.0


def _side(aliases: list[tuple[str, str]]) -> str | None:
    if any(s == "telegram" and i.startswith("user:") for s, i in aliases):
        return "telegram"
    if any((s == "gmail" and i.lower().endswith(WORK_DOMAIN))
           or (s == "slack" and i.startswith("user:")) for s, i in aliases):
        return "work"
    return None


async def _people_index() -> dict[str, list[tuple[int, str]]]:
    """name_key → [(id, сторона)]: сторона `telegram` или `work`."""
    global _index_at
    if _index and time.monotonic() - _index_at < _INDEX_TTL_S:
        return _index
    async with get_session() as s:
        rows = (await s.execute(
            select(EntityRow.id, EntityRow.name, EntityAliasRow.source,
                   EntityAliasRow.identifier)
            .join(EntityAliasRow, EntityAliasRow.entity_id == EntityRow.id)
            .where(EntityRow.type == "person",
                   EntityAliasRow.source.in_(("telegram", "gmail", "slack"))))).all()
    names: dict[int, str] = {}
    aliases: dict[int, list[tuple[str, str]]] = {}
    for eid, name, source, ident in rows:
        names[eid] = name
        aliases.setdefault(eid, []).append((source, ident))
    _index.clear()
    for eid, name in names.items():
        side, key = _side(aliases[eid]), name_key(name)
        if side and key:
            _index.setdefault(key, []).append((eid, side))
    _index_at = time.monotonic()
    return _index


async def suggest_person_twin(entity_id: int, name: str) -> int | None:
    """Если у этого человека ровно один тёзка с ДРУГОЙ стороны (Telegram ↔
    работа) — записать пару на ручной разбор. → id двойника или None."""
    if entity_id in _checked:
        return None
    _checked.add(entity_id)
    key = name_key(name)
    if not key:
        return None
    index = await _people_index()
    mine = next((side for eid, side in index.get(key, []) if eid == entity_id), None)
    others = [eid for eid, side in index.get(key, []) if side != mine and eid != entity_id]
    if mine is None or len(others) != 1 or len(index[key]) != 2:
        return None
    await propose_merge(
        entity_id, others[0], confidence=0.75,
        reason="то же полное имя (с точностью до транслита): Telegram и рабочая "
               "почта/Slack, третьих с таким именем нет")
    log.info("graph: двойник %s ↔ %s предложен владельцу", entity_id, others[0])
    return others[0]


async def suggest_chat_twin(entity_id: int, name: str) -> int | None:
    """Супергруппа и ровно одна группа с тем же именем — вероятная миграция."""
    if entity_id in _checked:
        return None
    _checked.add(entity_id)
    key = plain_name(name)
    if not key:
        return None
    async with get_session() as s:
        rows = (await s.execute(select(EntityRow.id, EntityRow.name).where(
            EntityRow.type == "group"))).all()
    twins = [eid for eid, other in rows if plain_name(other) == key]
    if len(twins) != 1:
        return None
    await propose_merge(entity_id, twins[0], confidence=0.6,
                        reason="группа и супергруппа с одним именем — вероятно, миграция")
    return twins[0]
