"""Единственная точка записи события источника. Дедуп атомарный.

Раньше каждый ингестор носил свою копию: `SELECT source_event_id ... IN (:ids)`,
потом `s.add()` для отсутствующих. Это check-then-insert — ровно та гонка, от
которой шлюз лечили через `ON CONFLICT` (см. gateway/events.py). У gmail она не
теоретическая: scripts/gmail_backfill.py вставляет тем же способом и ходит
параллельно поллеру, так что вместо безобидного дубля получался IntegrityError
и потеря всей пачки транзакции.

Спецификация события — kwargs `EventRow`. Обязательны source, source_event_id
и occurred_at; остальное на усмотрение источника.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from vera_shared.db.engine import get_session
from vera_shared.db.models import EventRow

log = logging.getLogger(__name__)

REQUIRED_FIELDS = ("source", "source_event_id", "occurred_at")


def _insert_for(session: Any):
    """ON CONFLICT DO NOTHING есть в обоих диалектах, но в разных модулях.

    Прод — Postgres; SQLite нужен, чтобы дедуп проверялся настоящей базой в
    тестах, а не моком — как JsonType и BigIntPk в db/models.py.
    """
    bind = getattr(session, "bind", None)
    dialect = getattr(getattr(bind, "dialect", None), "name", "postgresql")
    return sqlite_insert if dialect == "sqlite" else pg_insert


def valid_spec(spec: dict[str, Any]) -> bool:
    missing = [key for key in REQUIRED_FIELDS if not spec.get(key)]
    if missing:
        log.error("событие без %s — отброшено (source=%r)",
                  ", ".join(missing), spec.get("source"))
        return False
    return True


async def insert_events(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Вставить события, вернуть только реально вставленные (с `event_id`).

    Повтор уже виденного `(source, source_event_id)` молча пропускается — это
    и есть дедуп, и он безопасен при параллельных писателях.
    """
    usable = [spec for spec in specs if valid_spec(spec)]
    if not usable:
        return []

    fresh: list[dict[str, Any]] = []
    async with get_session() as s:
        insert = _insert_for(s)
        for spec in usable:
            stmt = (
                insert(EventRow)
                .values(triage_status="pending", **spec)
                .on_conflict_do_nothing(index_elements=["source", "source_event_id"])
                .returning(EventRow.id)
            )
            event_id = (await s.execute(stmt)).scalar_one_or_none()
            if event_id is not None:
                fresh.append({**spec, "event_id": event_id})
    if fresh:
        log.info("%s: %d новых событий", fresh[0]["source"], len(fresh))
    return fresh
