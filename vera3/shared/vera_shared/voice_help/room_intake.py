"""Завести срочную задачу в комнате и сообщить о ней — идемпотентно по command_id.

Дедуп — только через `task_id` (`help-<command_id>`) и `message_id` поста:
`open_task` возвращает существующую задачу как есть, `post_message` того же
id с тем же телом — дубль. Повтор слушателя, ретрай бота, перезапуск —
одна задача и один пост. Поэтому тело поста детерминировано: без времени.

В комнату текст уходит только через `redact_secrets`.
"""
from __future__ import annotations

from typing import Any

from vera_shared.redact import redact_secrets
from vera_shared.room.messages import post_message
from vera_shared.room.tasks import open_task
from vera_shared.voice_help.policy import (
    AGENT,
    ESCALATE_TO,
    RESPONSIBLE,
    SAFETY_NOTE,
    help_room,
    help_task_id,
    project_for,
)

TITLE_CHARS = 80
ACTION_CHARS = 1500


def source_ref(source: dict[str, Any] | None) -> str:
    src = source or {}
    return f"{src.get('session_id') or '?'}@{src.get('start')}-{src.get('end')}"


async def open_help_task(*, command_id: str, event_id: int | None, instruction: str,
                         source: dict[str, Any] | None) -> str:
    """→ task_id. Повторный вызов ничего не создаёт и не публикует второй раз."""
    room, task_id = help_room(), help_task_id(command_id)
    clean = redact_secrets(instruction)
    src = source or {}
    refs = [{"kind": "voice_command", "ref": command_id},
            {"kind": "source", "ref": source_ref(src)[:500]}]
    if event_id is not None:
        refs.insert(0, {"kind": "voice_event", "ref": str(event_id)})
    await open_task(
        room=room, task_id=task_id, agent=AGENT,
        title=f"Срочно (голос): {clean[:TITLE_CHARS]}", paths=None,
        project=project_for(src.get("app"), src.get("window_title")),
        priority=0, auto_pickup=False, responsible=RESPONSIBLE,
        next_action=f"{clean[:ACTION_CHARS]}\n\n{SAFETY_NOTE}", refs=refs)
    await post_message(
        room=room, message_id=task_id, from_agent=AGENT, task_id=task_id,
        status="request",
        body=f"Срочная просьба владельца голосом: {clean[:ACTION_CHARS]}\n\n{SAFETY_NOTE}")
    return task_id


async def escalate(*, task_id: str) -> None:
    await post_message(
        room=help_room(), message_id=f"{task_id}-escalate", from_agent=AGENT,
        to_agent=ESCALATE_TO, task_id=task_id, status="request",
        body=("Срочную просьбу владельца никто не взял за 5 минут — "
              f"посмотри задачу {task_id} и назначь исполнителя. {SAFETY_NOTE}"))
