"""Делегированное подтверждение: dot заверяет, что владелец признал авторство ОДНОЙ голосовой просьбы.

Не ответ владельца: ответ пишется как `dot-attested`, а `lift_hold` ждёт только `owner`,
так что ref `source_confirmed` не появится. Исполнитель видит `source_attested_by_dot`
и `attest_scope: text_reply_owner`; сама просьба обязана пройти детерминированный отбор
«простой текстовый ответ владельцу». Одна транзакция под блокировкой строки задачи.
Идемпотентность — по событию `delegated_attestation` (ключ и хэш запроса лежат в его data).
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from typing import Any

from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models import EventRow
from vera_shared.db.models_room import RoomTaskEventRow, RoomTaskQuestionRow
from vera_shared.db.models_voice import VoiceCommandRow
from vera_shared.office.repository import revision_of
from vera_shared.redact import redact_secrets
from vera_shared.room import questions
from vera_shared.room.task_events import record_event
from vera_shared.room.tasks import TaskNotFound, _locked
from vera_shared.voice_help.hold import lift_attested
from vera_shared.voice_help.policy import (
    AGENT,
    UNCERTAIN_QUESTION,
    attest_reply_text,
    help_task_id,
)

ATTESTED_BY = "dot-attested"
CLASSIFICATION = "owner_direct_in_dot_chat"
MAX_QUOTE = 500
MAX_ID = 128


class AttestError(Exception):
    code = "invalid"


class AttestForbidden(AttestError):
    code = "forbidden"


class AttestDisabled(AttestError):
    code = "disabled"


class InvalidScope(AttestError):
    code = "invalid_scope"


class InvalidAttestation(AttestError):
    code = "invalid"


class AttestConflict(AttestError):
    code = "conflict"


def enabled() -> bool:
    return os.environ.get("ROOM_ATTEST_ENABLED", "0") == "1"


def allowed_actors() -> set[str]:
    raw = os.environ.get("ROOM_ATTEST_ACTORS", "dot")
    return {a.strip() for a in raw.split(",") if a.strip()}


def _clean_quote(evidence_message_id: str, evidence_at: str, quoted_text: str,
                 classification: str, idempotency_key: str) -> str:
    if classification != CLASSIFICATION:
        raise InvalidAttestation(f"classification must be {CLASSIFICATION!r}")
    for name, v in (("evidence_message_id", evidence_message_id),
                    ("idempotency_key", idempotency_key)):
        if not v or len(v) > MAX_ID:
            raise InvalidAttestation(f"{name} must be 1..{MAX_ID} characters")
    try:
        when = datetime.fromisoformat(evidence_at)
    except ValueError:
        raise InvalidAttestation("evidence_at must be an ISO timestamp") from None
    if when.tzinfo is None or when.utcoffset() is None:
        raise InvalidAttestation("evidence_at must include a timezone")
    quote = redact_secrets(quoted_text.strip())
    if not quote or len(quote) > MAX_QUOTE:
        raise InvalidAttestation(f"quoted_text must be 1..{MAX_QUOTE} characters")
    return quote


def _payload_hash(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


async def _prior(s: Any, room: str, task_id: str, qid: int) -> RoomTaskEventRow | None:
    ev = RoomTaskEventRow
    rows = (await s.execute(select(ev).where(
        ev.room == room, ev.task_id == task_id,
        ev.kind == "delegated_attestation").order_by(ev.id))).scalars().all()
    return next((r for r in rows if (r.data or {}).get("qid") == qid), None)


async def _check_instruction(s: Any, task_id: str,
                             refs: list[dict[str, str]]) -> str:
    command_ids = [r["ref"] for r in refs if r.get("kind") == "voice_command"]
    if len(command_ids) != 1 or help_task_id(command_ids[0]) != task_id:
        raise InvalidScope("voice command does not match this task")
    command_id = command_ids[0]
    row = await s.get(VoiceCommandRow, command_id) if command_id else None
    if row is None or row.task_id != task_id or row.event_id is None:
        raise InvalidScope("original voice instruction not found")
    event = await s.get(EventRow, row.event_id)
    prefix = "Голосовое поручение Вере: "
    if (event is None or event.source != "voice_command"
            or event.source_event_id != command_id or event.category != "command"
            or not event.content_text.startswith(prefix)):
        raise InvalidScope("original voice event does not match this command")
    instruction = event.content_text[len(prefix):]
    if row.instruction and row.instruction != instruction:
        raise InvalidScope("voice queue and source event disagree")
    reply = attest_reply_text(redact_secrets(instruction))
    if reply is None:
        raise InvalidScope("instruction is not a literal one-word reply to the owner")
    return reply


async def attest(*, principal: str, room: str, task_id: str, qid: int,
                 expected_revision: int, evidence_message_id: str, evidence_at: str,
                 quoted_text: str, classification: str, idempotency_key: str,
                 ) -> dict[str, Any]:
    """→ {"ok", "replayed", "qid", "revision"}."""
    if principal not in allowed_actors():
        raise AttestForbidden(f"{principal!r} may not attest")
    if not enabled():
        raise AttestDisabled("delegated attestation is disabled")
    quote = _clean_quote(evidence_message_id, evidence_at, quoted_text, classification,
                         idempotency_key)
    digest = _payload_hash(room, task_id, qid, expected_revision, evidence_message_id,
                           evidence_at, quote)
    async with get_session() as s:
        task = await _locked(s, room, task_id)
        if task is None:
            raise TaskNotFound(room, task_id)
        if not task.task_id.startswith("help-vc-") or not any(
                r.get("kind") == "voice_command" for r in task.refs or []):
            raise InvalidScope("not a voice help task")
        prior = await _prior(s, room, task_id, qid)
        if prior is not None:
            data = prior.data or {}
            if data.get("idempotency_key") == idempotency_key:
                if data.get("payload_hash") != digest:
                    raise AttestConflict("idempotency_key reused with a different payload")
                return {"ok": True, "replayed": True, "qid": qid,
                        "revision": data.get("revision")}
            raise AttestConflict(f"question {qid} is already attested")
        if task.status != "open" or task.lease_holder is not None:
            raise InvalidScope("task must be open and unclaimed before attestation")
        q = await s.get(RoomTaskQuestionRow, qid, with_for_update=True)
        if (q is None or (q.room, q.task_id) != (room, task_id) or q.asked_by != AGENT
                or q.question != UNCERTAIN_QUESTION):
            raise InvalidScope("not the authorship question of this task")
        if q.status != "open":
            raise InvalidScope(f"question is {q.status}")
        reply = await _check_instruction(s, task_id, task.refs or [])
        current = await revision_of(s, room, task_id)
        if current != expected_revision:
            raise AttestConflict(f"revision is {current}, expected {expected_revision}")
        await questions.answer(room=room, task_id=task_id, qid=qid, by=ATTESTED_BY, s=s,
                               text=f"Подтверждено через dot: {quote}")
        await lift_attested(s, task, reply)
        await record_event(
            s, room=room, task_id=task_id, kind="delegated_attestation", agent=principal,
            text=quote, data={
                "qid": qid, "evidence_message_id": evidence_message_id,
                "evidence_at": evidence_at, "quoted_text": quote,
                "classification": classification, "attested_by": principal,
                "allowed_reply": reply,
                "revision": current, "idempotency_key": idempotency_key,
                "payload_hash": digest})
        return {"ok": True, "replayed": False, "qid": qid, "revision": current}
