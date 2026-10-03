"""Хранилище вывода ролей пары (миграция 045): запись результата, чтение для карточки.

Чтение терпит отсутствие таблиц (код деплоится раньше миграции): пусто. Запись пары — одной
транзакцией: старые роли пары удаляются, новые вставляются, строка прогона обновляется.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import bindparam, delete, select, text
from sqlalchemy.exc import DBAPIError

from vera_shared.db.engine import get_session
from vera_shared.db.models_graph import PairStatsRow
from vera_shared.db.models_pair_roles import PairRoleInferenceRow, PairRoleRunRow
from vera_shared.graph.pair_roles_queue import Pair, RunInfo
from vera_shared.graph.pair_roles_types import PairInference
from vera_shared.graph.pair_stats import PairStats, ordered
from vera_shared.timeutil import utc_naive_now

log = logging.getLogger(__name__)
_COLUMNS = ("entity_a, entity_b, predicate, direction, confidence, rationale, quotes, "
            "model, computed_at")


@dataclass(frozen=True)
class StoredRole:
    entity_a: int
    entity_b: int
    predicate: str
    direction: str
    confidence: float
    rationale: str
    quotes: tuple[str, ...]
    model: str
    computed_at: datetime | None


def _stored(row: Any) -> StoredRole:
    quotes = row["quotes"]
    if isinstance(quotes, str):
        quotes = json.loads(quotes)
    at = row["computed_at"]
    return StoredRole(row["entity_a"], row["entity_b"], row["predicate"], row["direction"],
                      float(row["confidence"]), row["rationale"] or "", tuple(quotes or ()),
                      row["model"] or "", datetime.fromisoformat(at) if isinstance(at, str) else at)


async def save_inference(inference: PairInference, marker: str) -> None:
    """Заменить роли пары и обновить строку прогона (роли пишутся, только если они есть)."""
    a, b = ordered(inference.entity_a, inference.entity_b)
    now = utc_naive_now()
    async with get_session() as s:
        await s.execute(delete(PairRoleInferenceRow).where(
            PairRoleInferenceRow.entity_a == a, PairRoleInferenceRow.entity_b == b))
        s.add_all(PairRoleInferenceRow(
            entity_a=a, entity_b=b, predicate=r.predicate, direction=r.direction,
            confidence=r.confidence, rationale=r.rationale, quotes=list(r.quotes),
            model=inference.model, evidence_hash=inference.digest, computed_at=now)
            for r in inference.roles)
        run = await s.get(PairRoleRunRow, (a, b))
        if run is None:
            run = PairRoleRunRow(entity_a=a, entity_b=b, evidence_hash=inference.digest)
            s.add(run)
        run.evidence_hash, run.pair_marker, run.summary = inference.digest, marker, inference.summary
        run.roles_found, run.model, run.cost_usd, run.computed_at = (
            len(inference.roles), inference.model, inference.cost_usd, now)
        run.failures, run.retry_after = 0, None


BACKOFF_BASE_HOURS = 1
BACKOFF_MAX_DAYS = 7


def backoff(failures: int) -> timedelta:
    return min(timedelta(hours=BACKOFF_BASE_HOURS * 2 ** min(max(0, failures - 1), 12)),
               timedelta(days=BACKOFF_MAX_DAYS))


async def save_failure(a: int, b: int, marker: str, reason: str) -> None:
    """Ответ модели не по схеме: роли пары не трогаем, ставим паузу (растёт со сбоями подряд)."""
    low, high = ordered(a, b)
    now = utc_naive_now()
    async with get_session() as s:
        run = await s.get(PairRoleRunRow, (low, high))
        if run is None:
            run = PairRoleRunRow(entity_a=low, entity_b=high, evidence_hash="", failures=0)
            s.add(run)
        run.failures = (run.failures or 0) + 1
        run.retry_after = now + backoff(run.failures)
        run.pair_marker, run.summary, run.computed_at = marker, f"сбой формата: {reason}"[:300], now
        run.evidence_hash = ""             # после паузы пара пересчитывается, даже если улики те же


async def touch_run(a: int, b: int, marker: str) -> None:
    """Пакет не изменился: не звать модель, но записать, что проверили."""
    low, high = ordered(a, b)
    async with get_session() as s:
        run = await s.get(PairRoleRunRow, (low, high))
        if run is not None:
            run.pair_marker, run.computed_at = marker, utc_naive_now()


async def last_run(a: int, b: int) -> RunInfo | None:
    low, high = ordered(a, b)
    async with get_session() as s:
        try:
            run = await s.get(PairRoleRunRow, (low, high))
        except DBAPIError as e:
            log.warning("pair_role_runs не прочитана (миграция 045?): %s", e)
            return None
    return (RunInfo(run.evidence_hash, run.pair_marker, run.computed_at, run.retry_after)
            if run else None)


async def all_runs() -> dict[Pair, RunInfo]:
    async with get_session() as s:
        try:
            rows = (await s.execute(select(PairRoleRunRow))).scalars().all()
        except DBAPIError as e:
            log.warning("pair_role_runs не прочитана (миграция 045?): %s", e)
            return {}
    return {(r.entity_a, r.entity_b): RunInfo(r.evidence_hash, r.pair_marker, r.computed_at,
                                              r.retry_after) for r in rows}


async def all_pair_stats() -> dict[Pair, PairStats]:
    async with get_session() as s:
        rows = (await s.execute(select(PairStatsRow))).scalars().all()
    return {(r.entity_a, r.entity_b): PairStats(
        r.dm_msgs, r.dm_days, r.mail_msgs, r.mail_days, r.co_days, r.work_co_days, r.co_chats,
        r.shared_groups, r.active_days, r.first_at, r.last_at) for r in rows}


async def roles_of(entity_id: int) -> dict[int, list[StoredRole]]:
    """Роли пар сущности: собеседник → роли."""
    async with get_session() as s:
        try:
            rows = (await s.execute(text(
                f"SELECT {_COLUMNS} FROM pair_role_inferences "
                "WHERE entity_a = :e OR entity_b = :e"), {"e": entity_id})).mappings().all()
        except DBAPIError as e:
            log.warning("pair_role_inferences не прочитана (миграция 045?): %s", e)
            return {}
    out: dict[int, list[StoredRole]] = {}
    for row in rows:
        role = _stored(row)
        out.setdefault(role.entity_b if role.entity_a == entity_id else role.entity_a, []).append(role)
    return out


async def roles_within(ids: list[int]) -> dict[Pair, list[StoredRole]]:
    unique = sorted(set(ids))
    if len(unique) < 2:
        return {}
    async with get_session() as s:
        try:
            rows = (await s.execute(
                text(f"SELECT {_COLUMNS} FROM pair_role_inferences "
                     "WHERE entity_a IN :ids AND entity_b IN :ids")
                .bindparams(bindparam("ids", expanding=True)), {"ids": unique})).mappings().all()
        except DBAPIError as e:
            log.warning("pair_role_inferences не прочитана (миграция 045?): %s", e)
            return {}
    out: dict[Pair, list[StoredRole]] = {}
    for row in rows:
        role = _stored(row)
        out.setdefault((role.entity_a, role.entity_b), []).append(role)
    return out
