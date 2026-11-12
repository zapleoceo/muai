"""Office intake: ответ на вопрос своей задачи и отметка доставленной ревизии."""
from __future__ import annotations

from typing import Any

from vera_shared.db.engine import get_session
from vera_shared.office import repository as repo
from vera_shared.office.acl import OfficePrincipal, authorize
from vera_shared.office.intake_common import (
    CLOSED,
    IntakeConflict,
    IntakeNotFound,
    office_room,
    own_task,
)
from vera_shared.office.intake_input import ID_RE, InvalidInput, clean_text, text_hash
from vera_shared.room import questions
from vera_shared.room.tasks import _locked
from vera_shared.timeutil import utc_naive_now


def _replay(prior: Any, qid: int, digest: str, task_id: str, current: int) -> dict[str, Any]:
    if prior.qid != qid or prior.text_hash != digest:
        raise IntakeConflict("payload_mismatch",
                             "idempotency_key already used with a different answer")
    return {"task_id": task_id, "question_id": qid, "status": "answered",
            "revision": prior.revision_after or current, "replayed": True}


async def answer(principal: OfficePrincipal, task_id: str, question_id: int, text: str,
                 expected_revision: int, idempotency_key: str) -> dict[str, Any]:
    """Ответ на открытый вопрос своей незакрытой задачи; повтор ключа — тот же результат.

    Строка задачи блокируется (FOR UPDATE) до проверки ревизии и записи: два параллельных
    ответа сериализуются, второй видит новую ревизию и получает 409.
    """
    authorize(principal, "answer")
    if not isinstance(idempotency_key, str) or not ID_RE.match(idempotency_key):
        raise InvalidInput("idempotency_key must match [A-Za-z0-9._:-]{1,128}")
    text = clean_text(text, "text", questions.MAX_ANSWER_CHARS)
    digest = text_hash(text)
    origin, _ = await own_task(principal, office_room(), task_id)
    room = origin.room
    async with get_session() as s:
        task = await _locked(s, room, task_id)
        if task is None:
            raise IntakeNotFound("task not found")
        prior = await repo.answer_key(s, task_id, idempotency_key)
        current = await repo.revision_of(s, room, task_id)
        if prior is not None:
            return _replay(prior, question_id, digest, task_id, current)
        if task.status in CLOSED:
            raise IntakeConflict("task_closed", f"task is {task.status}; answers are closed")
        if current != expected_revision:
            raise IntakeConflict("revision_mismatch", f"current revision is {current}")
        open_ids = {q["qid"] for q in await questions.questions_of(room, task_id)
                    if q["status"] == "open"}
        if question_id not in open_ids:
            raise IntakeNotFound("open question not found")
        # ответ, событие, outbox, updated_at и ключ фиксируются одной транзакцией
        await questions.answer(room=room, task_id=task_id, qid=question_id, text=text,
                               by=f"svc-{principal.name}", s=s)
        revision = await repo.revision_of(s, room, task_id)
        task.updated_at = utc_naive_now()  # updated_since должен увидеть ответ
        await repo.insert_answer_key(
            s, room=room, task_id=task_id, idempotency_key=idempotency_key, qid=question_id,
            text_hash=digest, revision_after=revision)
    return {"task_id": task_id, "question_id": question_id, "status": "answered",
            "revision": revision, "replayed": False}


async def mark_delivered(principal: OfficePrincipal, task_id: str, revision: int,
                         ) -> dict[str, Any]:
    """Монотонная отметка «источник получил ревизию»; повтор не меняет результат."""
    authorize(principal, "delivered")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise InvalidInput("revision must be a non-negative integer")
    origin, _ = await own_task(principal, office_room(), task_id)
    async with get_session() as s:
        task = await _locked(s, origin.room, task_id)
        if revision > await repo.revision_of(s, origin.room, task_id):
            raise IntakeConflict("revision_ahead", "revision is ahead of the task")
        before = origin.delivered_revision
        delivered = await repo.raise_delivered(s, origin.room, task_id, revision)
        if task is not None and delivered != before:
            task.updated_at = utc_naive_now()  # delivered_revision виден в списке
    return {"task_id": task_id, "delivered_revision": delivered}
