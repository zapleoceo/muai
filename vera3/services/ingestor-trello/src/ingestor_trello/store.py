"""Запись в БД: доски, события, авторы. Весь SQL Trello-ингестора здесь."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from vera_shared.db.engine import get_session
from vera_shared.db.models import EventRow
from vera_shared.db.models_sources import TrelloBoardRow
from vera_shared.graph.repo import upsert_entity

log = logging.getLogger("trello")


async def upsert_boards(boards: list[dict[str, Any]]) -> list[TrelloBoardRow]:
    """Синхронизирует список досок. Новые появляются сами, закрытые гаснут."""
    seen = {str(b["id"]): str(b.get("name") or "") for b in boards if b.get("id")}
    async with get_session() as s:
        rows = list((await s.execute(select(TrelloBoardRow))).scalars().all())
        known = {r.board_id: r for r in rows}
        for board_id, name in seen.items():
            row = known.get(board_id)
            if row is None:
                row = TrelloBoardRow(board_id=board_id, name=name, is_active=True)
                s.add(row)
                rows.append(row)
                log.info("trello: новая доска «%s»", name)
            else:
                row.name = name
                row.is_active = True
        for row in rows:
            if row.board_id not in seen:
                row.is_active = False
    return [r for r in rows if r.is_active]


async def save_cursor(board_id: str, cursor: str | None, error: str | None) -> None:
    values: dict[str, Any] = {"last_polled_at": datetime.utcnow(), "last_error": error}
    if cursor:
        values["last_action_id"] = cursor
    async with get_session() as s:
        await s.execute(
            update(TrelloBoardRow)
            .where(TrelloBoardRow.board_id == board_id)
            .values(**values)
        )


async def insert_events(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Вставляет только те, которых ещё нет. Возвращает реально вставленные."""
    if not specs:
        return []
    ids = [sp["source_event_id"] for sp in specs]
    fresh: list[dict[str, Any]] = []
    async with get_session() as s:
        existing = set((await s.execute(
            select(EventRow.source_event_id).where(
                EventRow.source == "trello",
                EventRow.source_event_id.in_(ids),
            )
        )).scalars().all())
        for spec in specs:
            if spec["source_event_id"] in existing:
                continue
            s.add(EventRow(triage_status="pending", **spec))
            fresh.append(spec)
    return fresh


async def sync_authors(specs: list[dict[str, Any]]) -> None:
    """Участник Trello → person-сущность с alias (trello, username).

    Слияние с телеграм/гмейл-двойниками — существующим /entities/duplicates,
    здесь ничего своего про дедуп нет.
    """
    seen: set[str] = set()
    for spec in specs:
        meta = spec.get("metadata_") or {}
        username = meta.get("author_username")
        if not username or username in seen:
            continue
        seen.add(username)
        await upsert_entity(
            type="person",
            name=str(meta.get("author_label") or username),
            source="trello",
            identifier=str(username),
        )
