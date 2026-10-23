"""recover и start владельца (дизайн §2.3, §2.4, §2.6): смена владения без смены состояния работы.

`recover` снимает с задачи мёртвую аренду и ставит `recovery_hold`: никто не берёт её, пока
владелец не решит о старте (`start`). Статус, `owner`, ожидание, вопросы, паузы, `depends_on`,
`auto_pickup`, `priority`, `next_action`, `refs`, `note` не меняются. Ключей аренды в B1 нет
(PR-C1), поэтому `lease_key_hash` и журнал K здесь не трогаются.
Оба действия — за `owner_post_gate`, без аренды; защита от гонки — `expected_fencing_token`.
"""
from __future__ import annotations

from typing import Any, Literal

from vera_shared.db.engine import get_session
from vera_shared.room.owner_actions import FINAL_STATUSES, OWNER_AGENT
from vera_shared.room.owner_block import BY_OWNER, clear_all_wait, owner_wait, unresolved_questions
from vera_shared.room.task_events import record_event
from vera_shared.room.tasks import StaleLease, TaskBusy, TaskNotFound, _locked, task_dict
from vera_shared.timeutil import utc_naive_now

STOP_SOURCES = ("host_rebooted_observed", "session_closed_observed",
                "process_absent_observed", "job_cancel_receipt", "other")
RESIDUAL_RISK = ("Room cannot verify that the previous run and the processes it started are "
                 "stopped; if they are alive, their external actions will continue")
MAX_FIELD = 500
StartStatus = Literal["keep", "open"]
AutoPickup = Literal["keep", "on", "off"]


class RecoverRefused(TaskBusy):
    """Состояние задачи не позволяет recover/start; `code` — машинная причина (HTTP 409)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class UnresolvedQuestion(ValueError):
    """`start(status=open)` при вопросе в open или answered без ack (HTTP 422)."""


def _field(value: str | None, name: str, *, required: bool = False) -> str | None:
    value = (value or "").strip()
    if not value:
        if required:
            raise ValueError(f"{name} must not be empty")
        return None
    if len(value) > MAX_FIELD:
        raise ValueError(f"{name} is longer than {MAX_FIELD} characters")
    return value


def _check_override(row_session: str | None, run_ref: str | None, stop_source: str,
                    stop_ref: str | None, effects: str, residual_risk_ack: bool,
                    ) -> dict[str, Any]:
    """§2.6: обязательные поля ручного override; без них кнопки нет (ValueError -> 422)."""
    if stop_source not in STOP_SOURCES:
        raise ValueError(f"stop_source must be one of {', '.join(STOP_SOURCES)}")
    run = _field(run_ref, "run_ref") or row_session or "unknown"
    ref = _field(stop_ref, "stop_ref")
    if run == "unknown" and stop_source == "other":
        raise ValueError("run_ref unknown with stop_source other: no proof of a stopped run; "
                         "release cooperatively or cancel and open a new task")
    if stop_source == "other" and ref is None:
        raise ValueError("stop_ref is required when stop_source is other")
    if not residual_risk_ack:
        raise ValueError(f"residual_risk_ack is required: {RESIDUAL_RISK}")
    return {"run_ref": run, "stop_source": stop_source, "stop_ref": ref,
            "effects": _field(effects, "effects", required=True)}


async def recover(*, room: str, task_id: str, expected_fencing_token: int,
                  stop_source: str, effects: str, residual_risk_ack: bool,
                  run_ref: str | None = None, stop_ref: str | None = None,
                  actor: str = OWNER_AGENT) -> dict[str, Any]:
    """Снимает истёкшую аренду: `fencing+1`, `recovery_hold=true`, событие `recovered`."""
    async with get_session() as s:
        row = await _locked(s, room, task_id)
        if row is None:
            raise TaskNotFound(room, task_id)
        if row.fencing_token != expected_fencing_token:
            raise StaleLease(f"fencing_token {expected_fencing_token} is stale "
                             f"(current {row.fencing_token})")
        if row.status in FINAL_STATUSES:
            raise RecoverRefused("final", f"task {task_id!r} is {row.status}")
        now = utc_naive_now()
        if row.lease_holder and row.lease_until and row.lease_until > now:
            raise RecoverRefused("held", f"task {task_id!r} lease of {row.lease_holder} is "
                                 "alive; recover applies to an expired lease only")
        if not row.lease_holder:
            raise RecoverRefused("not_recoverable", f"task {task_id!r} has no holder to recover")
        proof = _check_override(row.holder_session, run_ref, stop_source, stop_ref, effects,
                                residual_risk_ack)
        prior = {"holder": row.lease_holder, "fencing_token": row.fencing_token,
                 "pending_handoff_to": row.pending_handoff_to}
        row.fencing_token = (row.fencing_token or 0) + 1
        row.lease_holder = row.lease_until = None
        row.holder_session = row.holder_account = None
        row.pending_handoff_to = None
        row.recovery_hold = True
        row.updated_at = now
        await record_event(s, room=room, task_id=task_id, kind="recovered", agent=OWNER_AGENT,
                           fencing_token=row.fencing_token,
                           text=f"recover: {proof['stop_source']}",
                           data={"by": BY_OWNER, "actor": actor, "source": "manual_override",
                                 "proof": "owner_attested", "external_effects_unverified": True,
                                 "residual_risk_ack": True, "prior": prior, **proof,
                                 "via": "dashboard"})
        await s.refresh(row)
        return task_dict(row)


async def start(*, room: str, task_id: str, expected_fencing_token: int,
                status: StartStatus = "keep", clear_wait: bool = False,
                auto_pickup: AutoPickup = "keep", reason: str | None = None,
                actor: str = OWNER_AGENT) -> dict[str, Any]:
    """Решение о старте после recover: снимает `recovery_hold`, меняет только отмеченное."""
    if status not in ("keep", "open") or auto_pickup not in ("keep", "on", "off"):
        raise ValueError("status is keep|open, auto_pickup is keep|on|off")
    note = _field(reason, "reason")
    async with get_session() as s:
        row = await _locked(s, room, task_id)
        if row is None:
            raise TaskNotFound(room, task_id)
        if row.fencing_token != expected_fencing_token:
            raise StaleLease(f"fencing_token {expected_fencing_token} is stale "
                             f"(current {row.fencing_token})")
        if row.status in FINAL_STATUSES:
            raise RecoverRefused("final", f"task {task_id!r} is {row.status}")
        if not row.recovery_hold:
            raise RecoverRefused("not_in_recovery_hold",
                                 f"task {task_id!r} has no recovery_hold to start from")
        if status == "open" and await unresolved_questions(s, room, task_id):
            raise UnresolvedQuestion(
                "unresolved_question: the task has a question in open or answered without ack; "
                "answer and ack it, or close it, before status=open (keep is allowed)")
        changes: dict[str, Any] = {}
        if clear_wait and (row.waiting_until or row.waiting_reason or owner_wait(row)):
            was = {"decision_owner": row.waiting_decision_owner, "reason": row.waiting_reason,
                   "until": row.waiting_until.isoformat() if row.waiting_until else None}
            clear_all_wait(row)
            await record_event(s, room=room, task_id=task_id, kind="wait_resolved",
                               agent=OWNER_AGENT, fencing_token=row.fencing_token, text=note,
                               data={"by": BY_OWNER, "actor": actor, "via": "start",
                                     "was": was})
            changes["wait_cleared"] = True
        if status == "open" and row.status != "open":
            changes["status"] = {"from": row.status, "to": "open"}
            row.status = "open"
        if auto_pickup != "keep":
            new = auto_pickup == "on"
            changes["auto_pickup"] = {"from": bool(row.auto_pickup), "to": new}
            row.auto_pickup = new
        row.recovery_hold = False
        row.updated_at = utc_naive_now()
        await record_event(s, room=room, task_id=task_id, kind="start_approved",
                           agent=OWNER_AGENT, fencing_token=row.fencing_token, text=note,
                           data={"by": BY_OWNER, "actor": actor, "status": status,
                                 "clear_wait": clear_wait, "auto_pickup": auto_pickup,
                                 "changes": changes, "via": "dashboard"})
        await s.refresh(row)
        return task_dict(row)
