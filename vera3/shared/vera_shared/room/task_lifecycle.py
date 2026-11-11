"""Жизненный цикл аренды задачи: захват (`claim`), правка под арендой (`update`),
освобождение (`release`)."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError

from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskRow
from vera_shared.room.claim_gate import answered_questions, check_new_lease
from vera_shared.room.owner_block import OwnerBlock, owner_block
from vera_shared.room.task_errors import TaskBusy
from vera_shared.room.task_events import record_event
from vera_shared.room.task_fields import clean_responsible, validate_depends_on, validate_project
from vera_shared.room.task_lease import (
    HELD_STATUSES,
    RELEASE_STATUSES,
    _require_lease,
    _unconfirmed_voice,
    apply_claim,
    clear_holder_wait,
)
from vera_shared.room.task_refs import validate_refs
from vera_shared.room.task_store import _locked
from vera_shared.room.task_view import task_dict
from vera_shared.timeutil import utc_naive_now
from vera_shared.voice_help.attest_state import attestation_for


async def claim(*, room: str, task_id: str, agent: str, lease_seconds: int,
                title: str | None = None, paths: list[str] | None = None,
                session: str | None = None, account: str | None = None,
                ) -> dict[str, Any]:
    try:
        async with get_session() as s:
            row = await _locked(s, room, task_id)
            # время — после блокировки строки: ожидание лока могло длиться дольше аренды
            now = utc_naive_now()
            if row is None:
                row = RoomTaskRow(room=room, task_id=task_id, title=title, created_by=agent,
                                  status="open", fencing_token=0, paths=paths or [])
                s.add(row)
                await record_event(s, room=room, task_id=task_id, kind="created",
                                   agent=agent, text=title)
            elif row.status in ("done", "cancelled"):
                raise TaskBusy(f"task {task_id!r} is {row.status}; open a new task_id")
            attested = bool(row is not None and await attestation_for(s, room, task_id))
            if (row is not None and _unconfirmed_voice(row.refs)
                    and not (attested and agent == "dot"
                             and title is None and paths is None)):
                raise TaskBusy("unconfirmed voice source awaits owner answer")
            if attested and (agent != "dot" or title is not None or paths is not None):
                raise TaskBusy("attested task may only be claimed by dot without edits")
            own_live = bool(row.lease_holder == agent and row.lease_until
                            and row.lease_until > now)
            if not own_live and (refusal := await check_new_lease(
                    s, row, now, agent=agent, via="claim")) is not None:
                raise TaskBusy(refusal.message)
            answered = await answered_questions(s, row)
            live = apply_claim(row, agent=agent, now=now, lease_seconds=lease_seconds,
                               session=session, account=account)
            if title is not None:
                row.title = title
            if paths is not None:
                row.paths = paths
            await record_event(s, room=room, task_id=task_id,
                               kind="heartbeat" if live else "claimed", agent=agent,
                               session=session, account=account,
                               fencing_token=row.fencing_token,
                               data=None if live or not answered
                               else {"answered_qids": [a["qid"] for a in answered]})
            await s.refresh(row)
            return {**task_dict(row), **({"answered_questions": answered} if answered else {})}
    except IntegrityError:
        raise TaskBusy(f"task {task_id!r} was created concurrently; retry the claim"
                       ) from None


async def update(*, room: str, task_id: str, agent: str, fencing_token: int,
                 status: str | None = None, note: str | None = None,
                 extend_seconds: int | None = None, next_action: str | None = None,
                 priority: int | None = None, refs: list[dict[str, Any]] | None = None,
                 project: str | None = None, depends_on: list[str] | None = None,
                 auto_pickup: bool | None = None,
                 responsible: str | None = None) -> dict[str, Any]:
    if priority is not None and not 0 <= priority <= 3:
        raise ValueError("priority must be 0..3 (0 most urgent, default 2)")
    clean_refs = validate_refs(refs) if refs is not None else None
    clean_deps = (validate_depends_on(task_id, depends_on)
                  if depends_on is not None else None)
    clean_project = validate_project(project) if project is not None else None
    clean_resp = clean_responsible(responsible)
    if status is not None and status not in HELD_STATUSES:
        raise ValueError(f"status while holding must be one of {', '.join(HELD_STATUSES)}")
    async with get_session() as s:
        row = _require_lease(await _locked(s, room, task_id), room, task_id, agent,
                             fencing_token)
        if (await attestation_for(s, room, task_id)
                and (agent != "dot" or status is not None or note is not None
                     or next_action is not None or priority is not None or refs is not None
                     or project is not None or depends_on is not None
                     or auto_pickup is not None or responsible is not None)):
            raise TaskBusy("attested task permits only dot lease renewal")
        if (_unconfirmed_voice(row.refs)
                and (auto_pickup is True or (clean_refs is not None
                                             and not _unconfirmed_voice(clean_refs)))):
            raise TaskBusy("unconfirmed voice source cannot remove hold")
        if status == "in_progress" and (await owner_block(s, row)).active:
            raise OwnerBlock(f"update: task {task_id!r} awaits the owner; "
                             "status in_progress needs the owner's resolution first")
        now = utc_naive_now()
        prev_status = row.status
        if status is not None:
            row.status = status
        if note is not None:
            row.note = note
        if extend_seconds is not None:
            row.lease_until = now + timedelta(seconds=extend_seconds)
        if next_action is not None:
            row.next_action = next_action
        if priority is not None:
            row.priority = priority
        if clean_refs is not None:
            row.refs = clean_refs
        if clean_project is not None:
            row.project = clean_project
        if clean_deps is not None:
            row.depends_on = clean_deps
        if auto_pickup is not None:
            row.auto_pickup = auto_pickup
        if clean_resp is not None:
            row.responsible = clean_resp
        row.updated_at = now
        if status is not None or note is not None:
            # прогресс — содержательная правка; одно продление аренды (heartbeat) его не двигает
            row.last_progress_at = now
            row.last_progress_text = note if note is not None else f"status: {status}"
            clear_holder_wait(row)
            kind = "progress"
            # переход блокировки пишется раньше progress: Гантт не рисует вспышку работы
            flip = ("blocked" if status == "blocked" and prev_status != "blocked" else
                    "unblocked" if prev_status == "blocked" and status == "in_progress"
                    else None)
            if flip:
                await record_event(s, room=room, task_id=task_id, kind=flip, agent=agent,
                                   session=row.holder_session, account=row.holder_account,
                                   fencing_token=fencing_token, data={"status": status})
        else:
            kind = "heartbeat"
        await record_event(s, room=room, task_id=task_id, kind=kind, agent=agent,
                           session=row.holder_session, account=row.holder_account,
                           fencing_token=fencing_token, text=note,
                           data={"status": status} if status is not None else None)
        await s.refresh(row)
        return task_dict(row)


async def release(*, room: str, task_id: str, agent: str, fencing_token: int,
                  status: str, note: str | None = None) -> dict[str, Any]:
    if status not in RELEASE_STATUSES:
        raise ValueError(f"release status must be one of {', '.join(RELEASE_STATUSES)}")
    async with get_session() as s:
        row = _require_lease(await _locked(s, room, task_id), room, task_id, agent,
                             fencing_token)
        if await attestation_for(s, room, task_id) and status != "open":
            raise TaskBusy("attested task needs receipt completion or open release")
        state = await owner_block(s, row)
        if state.active and status != "blocked":
            raise OwnerBlock(f"release({status}): task {task_id!r} awaits the owner "
                             f"({', '.join(state.reasons())}); release only to blocked")
        row.status = status
        row.lease_holder = None
        row.lease_until = None
        clear_holder_wait(row)
        row.pending_handoff_to = None
        if note is not None:
            row.note = note
        row.updated_at = utc_naive_now()
        await record_event(s, room=room, task_id=task_id,
                           kind="done" if status == "done" else "released", agent=agent,
                           session=row.holder_session, account=row.holder_account,
                           fencing_token=fencing_token, text=note, data={"status": status})
        await s.refresh(row)
        return task_dict(row)
