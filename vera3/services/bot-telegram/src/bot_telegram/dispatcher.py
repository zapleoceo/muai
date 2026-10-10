"""Диспетчер офиса: сообщает получателю, что в комнате ждёт задача проекта Vera.

Без LLM и без текста задачи сверх очищенного заголовка. Шаги одной задачи:
claim от имени `vera` → `room_post` получателю (id `dispatch-<task_id>`, повтор
того же — дубль) → ref `dispatched: <id>` → release в `open`, чтобы реальный
исполнитель взял её следующим. Ref ставится после поста: упади процесс между
ними, следующий проход повторит пост (тот же id и тело — дедуп) и поставит ref.

Счётчик попыток живёт в refs (`dispatch_attempts: N`); после `MAX_ATTEMPTS`
сбоев подряд задача уходит в `blocked`.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from vera_shared.redact import redact_secrets
from vera_shared.room import task_queue, tasks
from vera_shared.room.messages import post_message
from vera_shared.voice_help.policy import (
    AGENT,
    DEFAULT_PROJECT,
    ESCALATE_TO,
    RESULT_RECIPIENT_REF,
    help_room,
)

log = logging.getLogger(__name__)

DISPATCHED_PREFIX = "dispatched:"
ATTEMPTS_PREFIX = "dispatch_attempts:"
RECIPIENT_PREFIX = RESULT_RECIPIENT_REF.split(":")[0] + ":"
MAX_ATTEMPTS = 3
LEASE_S = 60
TITLE_CHARS = 120
BLOCKED_NOTE = "получатель не подтвердил получение"
_NAME = re.compile(r"^[a-z0-9_-]{1,32}$")


def _ref_value(refs: list[dict[str, Any]], prefix: str) -> str | None:
    for r in refs:
        ref = str(r.get("ref", ""))
        if ref.startswith(prefix):
            return ref[len(prefix):].strip()
    return None


def recipient_of(refs: list[dict[str, Any]]) -> str:
    name = (_ref_value(refs, RECIPIENT_PREFIX) or "").lower()
    return name if _NAME.match(name) else ESCALATE_TO


def dispatch_body(task: dict[str, Any]) -> str:
    title = redact_secrets(task.get("title") or "")[:TITLE_CHARS]
    return (f"Задача {task['task_id']} ждёт исполнителя "
            f"(origin: {task['created_by']}, проект {task.get('project')}). {title}").strip()


def _with(refs: list[dict[str, Any]], prefix: str, value: str) -> list[dict[str, Any]]:
    kept = [r for r in refs if not str(r.get("ref", "")).startswith(prefix)]
    return [*kept, {"kind": "source", "ref": f"{prefix} {value}"}]


async def _post(task: dict[str, Any]) -> str:
    message_id = f"dispatch-{task['task_id']}"
    await post_message(room=task["room"], message_id=message_id, from_agent=AGENT,
                       to_agent=recipient_of(task["refs"]), task_id=task["task_id"],
                       status="request", body=dispatch_body(task))
    return message_id


async def _record_failure(task: dict[str, Any], error: Exception) -> None:
    refs = task["refs"]
    attempts = int(_ref_value(refs, ATTEMPTS_PREFIX) or 0) + 1
    room, task_id, token = task["room"], task["task_id"], task["fencing_token"]
    log.warning("dispatcher: %s не доставлена (%s), попытка %d/%d", task_id,
                type(error).__name__, attempts, MAX_ATTEMPTS)
    await tasks.update(room=room, task_id=task_id, agent=AGENT, fencing_token=token,
                       refs=_with(refs, ATTEMPTS_PREFIX, str(attempts)))
    if attempts >= MAX_ATTEMPTS:
        await tasks.release(room=room, task_id=task_id, agent=AGENT, fencing_token=token,
                            status="blocked", note=BLOCKED_NOTE)
    else:
        await tasks.release(room=room, task_id=task_id, agent=AGENT, fencing_token=token,
                            status="open")


async def dispatch_one() -> bool:
    """Обработать одну недиспетчеризованную задачу. False — очередь пуста."""
    task = await task_queue.next_task(
        room=help_room(), agent=AGENT, project=DEFAULT_PROJECT, lease_seconds=LEASE_S,
        session=None, account=None, skip_ref_prefix=DISPATCHED_PREFIX)
    if task is None:
        return False
    try:
        try:
            message_id = await _post(task)
        except Exception as e:
            await _record_failure(task, e)
            return True
        await tasks.update(room=task["room"], task_id=task["task_id"], agent=AGENT,
                           fencing_token=task["fencing_token"],
                           refs=_with(task["refs"], DISPATCHED_PREFIX, message_id))
        await tasks.release(room=task["room"], task_id=task["task_id"], agent=AGENT,
                            fencing_token=task["fencing_token"], status="open")
        log.info("dispatcher: %s → %s (%s)", task["task_id"], recipient_of(task["refs"]),
                 message_id)
    except tasks.StaleLease:
        log.warning("dispatcher: аренда %s потеряна — пропускаю", task["task_id"])
    return True
