"""Какие пары пора судить моделью — чистая функция по `pair_stats` и прошлым прогонам.

Пара берётся, если она устоявшаяся (`is_established`: ≈8 дней контакта) и: её ещё не судили,
либо прошлый прогон старше `MAX_AGE_DAYS`, либо статистика пары изменилась (появились
сообщения / дни общения) и с прошлого прогона прошло не меньше `MIN_RERUN_DAYS` — иначе живая
переписка гоняла бы модель по одной и той же паре каждый цикл. Порядок: пары владельца первыми,
внутри — ещё не судённые, потом по силе общения. «Изменилась ли статистика» проверяется
дёшево по маркеру; точное «изменился ли пакет улик» решает хэш в `pair_roles.infer_pair`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from vera_shared.graph.connection_model import interaction_strength, is_established
from vera_shared.graph.pair_stats import PairStats

MIN_RERUN_DAYS = 3
MAX_AGE_DAYS = 30
Pair = tuple[int, int]


@dataclass(frozen=True)
class RunInfo:
    digest: str
    marker: str
    computed_at: datetime
    retry_after: datetime | None = None   # пауза после сбоя формата: до неё пару не берём


def marker_of(stats: PairStats) -> str:
    """Что в статистике пары говорит «появились новые улики»."""
    return f"{stats.dm_msgs}:{stats.mail_msgs}:{stats.co_days}:{stats.work_co_days}"


def _due(stats: PairStats, run: RunInfo | None, now: datetime) -> bool:
    if run is None:
        return True
    if run.retry_after is not None:
        return now >= run.retry_after
    age = now - run.computed_at
    if age >= timedelta(days=MAX_AGE_DAYS):
        return True
    return run.marker != marker_of(stats) and age >= timedelta(days=MIN_RERUN_DAYS)


def pick_pairs(stats: dict[Pair, PairStats], runs: dict[Pair, RunInfo], owner: int | None,
               limit: int, now: datetime) -> list[Pair]:
    """До `limit` пар к суду, самые нужные первыми."""
    due = [(pair, st) for pair, st in stats.items()
           if is_established(st) and _due(st, runs.get(pair), now)]
    due.sort(key=lambda item: (owner not in item[0], item[0] in runs,
                               -interaction_strength(item[1]), item[0]))
    return [pair for pair, _ in due[:limit]]
