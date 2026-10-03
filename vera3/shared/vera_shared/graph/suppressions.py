"""Отвергнутые владельцем выведенные роли пар (миграция 041).

Чтение терпит отсутствие таблицы (как `pair_stats`): без неё роли выводятся
как раньше. Запись — в сессии вызывающего, чтобы правка и строка журнала
шли одной транзакцией.
"""
from __future__ import annotations

import logging

from sqlalchemy import bindparam, delete, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.engine import get_session
from vera_shared.db.models_graph import ConnectionSuppressionRow
from vera_shared.graph.connection_model import INFERRED_PREDICATE
from vera_shared.graph.pair_stats import ordered

log = logging.getLogger(__name__)


async def suppressed_partners(entity_id: int) -> set[int]:
    """Собеседники, чью выведенную роль владелец отверг."""
    async with get_session() as s:
        try:
            rows = (await s.execute(text(
                "SELECT entity_a, entity_b FROM connection_suppressions "
                "WHERE predicate = :p AND (entity_a = :e OR entity_b = :e)"),
                {"p": INFERRED_PREDICATE, "e": entity_id})).all()
        except DBAPIError as e:
            log.warning("connection_suppressions не прочитана (миграция 041?): %s", e)
            return set()
    return {b if a == entity_id else a for a, b in rows}


async def suppressed_within(ids: list[int]) -> set[tuple[int, int]]:
    """Упорядоченные пары из `ids` с отвергнутой выведенной ролью."""
    unique = sorted(set(ids))
    if len(unique) < 2:
        return set()
    async with get_session() as s:
        try:
            rows = (await s.execute(
                text("SELECT entity_a, entity_b FROM connection_suppressions "
                     "WHERE predicate = :p AND entity_a IN :ids AND entity_b IN :ids")
                .bindparams(bindparam("ids", expanding=True)),
                {"p": INFERRED_PREDICATE, "ids": unique})).all()
        except DBAPIError as e:
            log.warning("connection_suppressions не прочитана: %s", e)
            return set()
    return {(a, b) for a, b in rows}


async def suppress_pair(s: AsyncSession, a: int, b: int) -> bool:
    """True — запись создана, False — пара уже была отвергнута."""
    low, high = ordered(a, b)
    try:
        async with s.begin_nested():
            s.add(ConnectionSuppressionRow(entity_a=low, entity_b=high,
                                           predicate=INFERRED_PREDICATE))
            await s.flush()
    except IntegrityError:
        return False
    return True


async def lift_suppression(s: AsyncSession, a: int, b: int) -> bool:
    low, high = ordered(a, b)
    res = await s.execute(delete(ConnectionSuppressionRow).where(
        ConnectionSuppressionRow.entity_a == low, ConnectionSuppressionRow.entity_b == high,
        ConnectionSuppressionRow.predicate == INFERRED_PREDICATE))
    return bool(res.rowcount)
