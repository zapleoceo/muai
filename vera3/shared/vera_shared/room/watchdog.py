"""Сторож задач комнаты: один проход `run_once` и запись состояния в `watchdog_state`.

Не зависит от исполнителей: читает только таблицы комнаты. Каждая задача
обрабатывается в своей транзакции под блокировкой строки; решения — в
`watchdog_rules.decide`. Сторож ничего не удаляет и не трогает паузу, вопрос
владельцу, готовые и отменённые задачи (attention их не пропускает в правила).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskEventRow, RoomTaskRow, WatchdogStateRow
from vera_shared.room.messages import add_message
from vera_shared.room.task_attention import attention_map
from vera_shared.room.task_events import record_event
from vera_shared.room.watchdog_rules import Action, decide
from vera_shared.timeutil import utc_naive_now

log = logging.getLogger(__name__)
STATE_NAME = "room"
AGENT = "watchdog"
CANDIDATE_STATUSES = ("in_progress", "blocked")


async def _candidates(s: AsyncSession) -> list[tuple[str, str]]:
    rows = (await s.execute(select(RoomTaskRow.room, RoomTaskRow.task_id).where(
        RoomTaskRow.status.in_(CANDIDATE_STATUSES),
        RoomTaskRow.lease_holder.is_not(None)))).all()
    return [(row[0], row[1]) for row in rows]


async def _context(s: AsyncSession, room: str, task_id: str,
                   ) -> tuple[datetime | None, tuple[str, int | None] | None, frozenset[str]]:
    ev = RoomTaskEventRow
    base = (ev.room == room, ev.task_id == task_id)
    claimed = (await s.execute(select(ev.at).where(
        *base, ev.kind.in_(("claimed", "handoff_accept"))).order_by(ev.id.desc()).limit(1)
    )).scalar_one_or_none()
    last = (await s.execute(select(ev.kind, ev.fencing_token).where(*base)
                            .order_by(ev.id.desc()).limit(1))).first()
    acts = (await s.execute(select(ev.data).where(*base, ev.kind == "watchdog_action")
                            )).scalars().all()
    seen = frozenset(d["checkpoint"] for d in acts if d and d.get("checkpoint"))
    return claimed, (tuple(last) if last else None), seen  # type: ignore[arg-type]


async def _apply(s: AsyncSession, row: RoomTaskRow, a: Action, now: datetime) -> None:
    await record_event(s, room=row.room, task_id=row.task_id, kind=a.kind,
                       fencing_token=row.fencing_token, text=a.text, data=a.data)
    if a.reopen:
        row.status = "open"
        row.lease_holder, row.lease_until = None, None
        row.pending_handoff_to = None
        row.updated_at = now
    if a.notify:
        await add_message(
            s, room=row.room, message_id=f"watchdog-{uuid.uuid4().hex[:16]}",
            from_agent=AGENT, to_agent=a.notify, task_id=row.task_id,
            body=f"Сторож: {a.text} ({row.title or row.task_id})", status="request")


async def check_task(room: str, task_id: str, now: datetime) -> list[Action]:
    async with get_session() as s:
        row = await s.get(RoomTaskRow, (room, task_id), with_for_update=True)
        if row is None or row.status not in CANDIDATE_STATUSES or not row.lease_holder:
            return []
        att = (await attention_map(s, [row], now))[(room, task_id)]
        claimed, last, seen = await _context(s, room, task_id)
        actions = decide(row, att, now=now, claimed_at=claimed, last_event=last,
                         seen_checkpoints=seen)
        for a in actions:
            await _apply(s, row, a, now)
        return actions


async def write_state(error: str | None, now: datetime) -> None:
    async with get_session() as s:
        row = await s.get(WatchdogStateRow, STATE_NAME)
        if row is None:
            row = WatchdogStateRow(name=STATE_NAME)
            s.add(row)
        row.last_run_at, row.last_error = now, error


async def save_state(error: str | None, now: datetime) -> None:
    """Сбой записи состояния логируется и не обрывает цикл: следующий проход повторит."""
    try:
        await write_state(error, now)
    except Exception:
        log.warning("watchdog: cannot write watchdog_state (error=%r)", error, exc_info=True)


async def run_once(now: datetime | None = None) -> dict[str, Any]:
    """Один проход. Ошибка задачи пишется в last_error и не останавливает остальные."""
    now = now or utc_naive_now()
    errors: list[str] = []
    done: list[tuple[str, str, str]] = []
    try:
        async with get_session() as s:
            ids = await _candidates(s)
    except Exception as e:
        log.warning("watchdog: cannot list tasks: %s", e)
        await save_state(f"{type(e).__name__}: {e}"[:500], now)
        return {"checked": 0, "actions": [], "error": str(e)}
    for room, task_id in ids:
        try:
            done += [(room, task_id, a.kind) for a in await check_task(room, task_id, now)]
        except Exception as e:
            log.warning("watchdog: task %s/%s failed: %s", room, task_id, e)
            errors.append(f"{room}/{task_id}: {type(e).__name__}: {e}")
    await save_state("; ".join(errors)[:500] or None, now)
    return {"checked": len(ids), "actions": done, "error": errors[0] if errors else None}
