"""Запись в БД: доски и авторы. Свой SQL источника — только про курсоры.

Вставка событий и «автор → сущность» переехали в `vera_shared.ingest`: они
одинаковы у всех источников, а копия дедупа здесь была check-then-insert.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from vera_shared.db.engine import get_session
from vera_shared.db.models_sources import TrelloBoardRow
from vera_shared.ingest import insert_events, sync_author_entities

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


def _author_of(spec: dict[str, Any]) -> dict[str, Any] | None:
    meta = spec.get("metadata_") or {}
    username = meta.get("author_username")
    if not username:
        return None
    return {"identifier": str(username),
            "name": str(meta.get("author_label") or username)}


async def save_events(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """События + person-сущности их авторов. → реально вставленные события.

    Слияние двойников с телеграм/гмейл — существующим /entities/duplicates,
    своего дедупа сущностей у источника нет.
    """
    fresh = await insert_events(specs)
    await sync_author_entities(fresh, source="trello", author_of=_author_of)
    return fresh
