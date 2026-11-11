"""Факты, которые агент поиска записал сам: не видны поиску, пока их не подтвердит владелец.

Агент читает недоверенный текст писем и чатов, и письмо «запомни, что …» могло
бы стать фактом, на который Вера потом опирается в ответах. Поэтому запись
агента ложится скрытой (`hidden`, прежний статус `pending`) с
`metadata.verified=false`: ни один путь поиска и ни один воркер скрытое
событие не берёт. Владелец подтверждает факт на `/approvals` дашборда — тогда
событие возвращается в `pending`, триаж его эмбеддит, и поиск начинает его
видеть. Подтверждение пишется в `mcp_audit`, `undo` скрывает факт обратно.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models import EventRow
from vera_shared.events import edit as event_edit
from vera_shared.events.visibility import HIDDEN_STATUS, hide_values
from vera_shared.journal import audit
from vera_shared.timeutil import utc_naive_now

log = logging.getLogger(__name__)

AGENT_MEMORY_SOURCE = "vera_memory"
#: Метка происхождения записи агента — по ней факт виден как агентский.
AGENT_WRITER = "search_agent"
VERIFIED_KEY = "verified"
REJECTED_KEY = "rejected"
PENDING_SCAN_LIMIT = 500


async def write_agent_memory(fact: str, tags: list[str], confidence: float) -> int:
    now = utc_naive_now()
    status, triage_meta = hide_values("pending", None)
    async with get_session() as s:
        ev = EventRow(
            source=AGENT_MEMORY_SOURCE,
            source_event_id=f"memory:{now.timestamp()}",
            account="vera",
            category="fact",
            content_text=fact,
            occurred_at=now,
            metadata_={"tags": tags, "confidence": confidence,
                       "written_by": AGENT_WRITER, VERIFIED_KEY: False},
            triage_status=status,
            triage_metadata=triage_meta,
        )
        s.add(ev)
        await s.flush()
        event_id = ev.id
    log.info("agent memory %s saved hidden until owner confirms", event_id)
    return event_id


def _awaits_review(row: EventRow) -> bool:
    meta = row.metadata_ or {}
    return (meta.get("written_by") == AGENT_WRITER and meta.get(VERIFIED_KEY) is False
            and not meta.get(REJECTED_KEY))


async def pending_agent_memories(limit: int = 100) -> list[EventRow]:
    async with get_session() as s:
        rows = (await s.execute(
            select(EventRow)
            .where(EventRow.source == AGENT_MEMORY_SOURCE,
                   EventRow.triage_status == HIDDEN_STATUS)
            .order_by(EventRow.id.desc()).limit(PENDING_SCAN_LIMIT)
        )).scalars().all()
    return [r for r in rows if _awaits_review(r)][:limit]


async def _decide(event_id: int, client: str, *, confirm: bool) -> dict[str, Any]:
    async with get_session() as s:
        row = await event_edit.load_row(s, event_id)
        if row.source != AGENT_MEMORY_SOURCE or not _awaits_review(row):
            raise ValueError(f"event {event_id} is not an agent memory awaiting review")
        patch = {VERIFIED_KEY: True} if confirm else {REJECTED_KEY: True}
        before, after = await event_edit.update_event(s, event_id, metadata_patch=patch)
        if confirm:
            _, after = await event_edit.set_hidden(s, event_id, hidden=False)
        # Имена существующих инструментов журнала: их откат уже умеет `undo`
        # (unhide_event → снова скрыть, update_event → вернуть метаданные).
        audit_id = await audit.record(
            s, client=client, tool="unhide_event" if confirm else "update_event",
            args={"event_id": event_id, "agent_memory": "confirm" if confirm else "reject"},
            kind="event", target_id=event_id, before=before, after=after)
    log.info("agent memory %s %s by owner", event_id, "confirmed" if confirm else "rejected")
    return {"ok": True, "audit_id": audit_id, "event_id": event_id}


async def confirm_agent_memory(event_id: int, client: str) -> dict[str, Any]:
    return await _decide(event_id, client, confirm=True)


async def reject_agent_memory(event_id: int, client: str) -> dict[str, Any]:
    return await _decide(event_id, client, confirm=False)
