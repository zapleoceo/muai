"""БД-часть переразбора: выборка, бэкап до записи, пакетное применение, откат."""
from __future__ import annotations

import json
import os
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from sqlalchemy import select, update
from vera_shared.db.engine import get_session
from vera_shared.db.models import EventEmbeddingRow, EventRow
from vera_shared.timeutil import utc_naive_now

from ingestor_gmail.reingest import Candidate, EventSnapshot, is_jira_event

_PAGE = 500


async def load_jira_events(
    event_ids: list[int] | None = None, since: datetime | None = None, limit: int | None = None,
) -> list[EventSnapshot]:
    """Письма Jira, новые первыми. Адрес проверяет is_jira_event; SQL лишь
    сужает выборку по подстроке «atlassian»."""
    found: list[EventSnapshot] = []
    after = 1 << 62
    while limit is None or len(found) < limit:
        stmt = (select(EventRow.id, EventRow.source_event_id, EventRow.account,
                       EventRow.content_text, EventRow.metadata_)
                .where(EventRow.source == "gmail", EventRow.id < after,
                       EventRow.content_text.ilike("%atlassian%"))
                .order_by(EventRow.id.desc()).limit(_PAGE))
        if event_ids:
            stmt = stmt.where(EventRow.id.in_(event_ids))
        if since:
            stmt = stmt.where(EventRow.occurred_at >= since)
        async with get_session() as s:
            rows = (await s.execute(stmt)).all()
        if not rows:
            break
        after = rows[-1][0]
        found.extend(ev for ev in (EventSnapshot(*r) for r in rows) if is_jira_event(ev))
    return found[:limit] if limit else found


async def embedded_ids(ids: list[int]) -> set[int]:
    have: set[int] = set()
    for i in range(0, len(ids), _PAGE):
        async with get_session() as s:
            have.update((await s.execute(select(EventEmbeddingRow.event_id).where(
                EventEmbeddingRow.event_id.in_(ids[i:i + _PAGE])))).scalars().all())
    return have


def write_backup(path: Path, batch: Iterable[Candidate]) -> None:
    """Строка на событие; fsync до первого UPDATE — иначе откату нечего читать."""
    with path.open("a", encoding="utf-8") as fh:
        for c in batch:
            fh.write(json.dumps({"id": c.event.id, "content_text": c.event.content_text,
                                 "metadata": c.event.metadata}, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


async def apply_batch(batch: list[Candidate]) -> int:
    """Одна транзакция на пачку. WHERE content_text = старый: событие, которое
    успели изменить после чтения, пропускается, а не затирается."""
    stamp = utc_naive_now().isoformat(timespec="seconds")
    done = 0
    async with get_session() as s:
        for c in batch:
            meta = {**(c.event.metadata or {}), "reingested_at": stamp}
            res = await s.execute(
                update(EventRow)
                .where(EventRow.id == c.event.id, EventRow.source == "gmail",
                       EventRow.content_text == c.event.content_text)
                .values(content_text=c.new_text, metadata_=meta))
            done += res.rowcount or 0
    return done


async def rollback(path: Path, batch_size: int = 50) -> int:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    restored = 0
    for i in range(0, len(rows), batch_size):
        async with get_session() as s:
            for r in rows[i:i + batch_size]:
                res = await s.execute(
                    update(EventRow)
                    .where(EventRow.id == r["id"], EventRow.source == "gmail")
                    .values(content_text=r["content_text"], metadata_=r["metadata"]))
                restored += res.rowcount or 0
    return restored
