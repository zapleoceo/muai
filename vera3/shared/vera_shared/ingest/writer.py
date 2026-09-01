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

    Вставка идёт ПАЧКОЙ: один многострочный INSERT вместо round-trip на
    событие. Поллер gmail тянет до MAX_PER_RUN=500 писем за прогон, то есть
    раньше это были 500 отдельных обращений к базе внутри одной транзакции.
    """
    usable = [spec for spec in specs if valid_spec(spec)]
    if not usable:
        return []

    async with get_session() as s:
        insert = _insert_for(s)
        # Многострочный VALUES требует одинакового набора колонок, а источники
        # заполняют разные поля. Не подставляем недостающие как NULL — это
        # затёрло бы server-side дефолты; вместо этого группируем по форме.
        # У одного источника форма обычно одна, так что групп почти всегда 1.
        by_shape: dict[frozenset[str], list[dict[str, Any]]] = {}
        for spec in usable:
            by_shape.setdefault(frozenset(spec), []).append(spec)

        new_ids: dict[tuple[str, str], int] = {}
        for group in by_shape.values():
            stmt = (
                insert(EventRow)
                .values([{"triage_status": "pending", **spec} for spec in group])
                .on_conflict_do_nothing(index_elements=["source", "source_event_id"])
                # id недостаточно: RETURNING отдаёт только вставленные строки и
                # НЕ говорит, какой из specs какой. Ключ дедупа — он же ключ
                # сопоставления.
                .returning(EventRow.id, EventRow.source, EventRow.source_event_id)
            )
            for row in (await s.execute(stmt)).all():
                new_ids[(row.source, row.source_event_id)] = row.id

    # Порядок входа сохраняем: вызывающие ходят по результату как по своей пачке
    fresh = [
        {**spec, "event_id": new_ids[key]}
        for spec in usable
        if (key := (spec["source"], spec["source_event_id"])) in new_ids
    ]
    if fresh:
        log.info("%s: %d новых событий", fresh[0]["source"], len(fresh))
    return fresh
