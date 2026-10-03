"""Взаимодействия пары людей: чтение кэша `pair_stats` и его пересборка.

Строки — производные от `events` и `memberships` (миграция 040); правды в них
нет, таблицу можно очистить и пересчитать `refresh_pair_stats`. Читают её
`connections` (карточка, граф, MCP) и чистка связей.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.exc import DBAPIError

from vera_shared.db.engine import get_session
from vera_shared.graph.pair_stats_sql import PAIR_STATS_COLUMNS, PAIR_STATS_SELECT

log = logging.getLogger(__name__)

#: Чат, где авторов больше, — публичная площадка, а не круг знакомых: «оба писали
#: в одном чате» там ничего не говорит. В живых данных 122 чата из 135 укладываются
#: в 60 авторов, остальные 13 — 80…2400 авторов и 80% всех (чат, человек, день).
MAX_CHAT_AUTHORS = 60
#: Чаты этих проектов (`project_membership`) считаются рабочими. Slack — всегда.
WORK_PROJECTS = ("itstep", "veranda")
REFRESH_LOCK_KEY = 7_340_040

_FIELDS = ("dm_msgs", "dm_days", "mail_msgs", "mail_days", "co_days", "work_co_days",
           "co_chats", "shared_groups", "active_days")
_COLUMNS = "entity_a, entity_b, " + ", ".join(_FIELDS) + ", first_at, last_at"


@dataclass(frozen=True)
class PairStats:
    dm_msgs: int = 0
    dm_days: int = 0
    mail_msgs: int = 0
    mail_days: int = 0
    co_days: int = 0
    work_co_days: int = 0
    co_chats: int = 0
    shared_groups: int = 0
    active_days: int = 0
    first_at: datetime | None = None
    last_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {**{f: getattr(self, f) for f in _FIELDS},
                "first_at": self.first_at.isoformat() if self.first_at else None,
                "last_at": self.last_at.isoformat() if self.last_at else None}


def ordered(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def _stats(row: Any) -> PairStats:
    return PairStats(**{f: int(row[f] or 0) for f in _FIELDS},
                     first_at=_when(row["first_at"]), last_at=_when(row["last_at"]))


def _when(value: Any) -> datetime | None:
    # Сырой text() на SQLite отдаёт строку, на Postgres — datetime.
    return datetime.fromisoformat(value) if isinstance(value, str) else value


async def partner_stats(entity_id: int) -> dict[int, PairStats]:
    """Все партнёры сущности → статистика пары. Пусто, если таблицы ещё нет."""
    async with get_session() as s:
        try:
            rows = (await s.execute(text(
                f"SELECT {_COLUMNS} FROM pair_stats "
                "WHERE entity_a = :e OR entity_b = :e"), {"e": entity_id})).mappings().all()
        except DBAPIError as e:
            log.warning("pair_stats не прочитана (миграция 040 накачена?): %s", e)
            return {}
    return {(r["entity_b"] if r["entity_a"] == entity_id else r["entity_a"]): _stats(r)
            for r in rows}


async def stats_within(ids: list[int]) -> dict[tuple[int, int], PairStats]:
    """Статистика всех пар, у которых ОБА конца в `ids` (ключ — упорядоченная пара)."""
    unique = sorted(set(ids))
    if len(unique) < 2:
        return {}
    async with get_session() as s:
        try:
            rows = (await s.execute(
                text(f"SELECT {_COLUMNS} FROM pair_stats "
                     "WHERE entity_a IN :ids AND entity_b IN :ids")
                .bindparams(bindparam("ids", expanding=True)), {"ids": unique})).mappings().all()
        except DBAPIError as e:
            log.warning("pair_stats не прочитана: %s", e)
            return {}
    return {(r["entity_a"], r["entity_b"]): _stats(r) for r in rows}


async def refresh_pair_stats(owner_tg_id: int | None = None) -> int | None:
    """Пересобрать таблицу одной транзакцией; читатели видят старую до коммита.
    Возвращает число пар, None — пересборку уже ведёт другая реплика (advisory-замок:
    без него два DELETE+INSERT подряд упёрлись бы в первичный ключ). Без владельца
    (нет `OWNER_TELEGRAM_ID`) личка не считается."""
    owner = str(owner_tg_id if owner_tg_id is not None
                else os.environ.get("OWNER_TELEGRAM_ID", "0"))
    params = {"owner": owner, "max_authors": MAX_CHAT_AUTHORS,
              "work_projects": list(WORK_PROJECTS)}
    async with get_session() as s:
        if not (await s.execute(text("SELECT pg_try_advisory_xact_lock(:k)"),
                                {"k": REFRESH_LOCK_KEY})).scalar_one():
            return None
        await s.execute(text("SET LOCAL statement_timeout = '120s'"))
        # Кэш, не правда: пересобирается целиком и атомарно — DELETE и INSERT в одной транзакции.
        await s.execute(text("DELETE FROM pair_stats"))
        res = await s.execute(text(
            f"INSERT INTO pair_stats ({PAIR_STATS_COLUMNS}) {PAIR_STATS_SELECT}"), params)
    return res.rowcount or 0
