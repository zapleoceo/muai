"""Задачи комнаты: аренда (lease) + fencing-токен, чтобы двое не правили одно и то же.

Захват свободной или просроченной задачи увеличивает fencing_token; продление
своей живой аренды его не меняет. Любая правка/освобождение требует текущий
токен и живую аренду: агент, у которого аренда истекла и задачу перехватили,
получает отказ, даже если «проснулся» и продолжил с того же места.

Фасад: реализация разложена по модулям, прежние импорты `room.tasks.<имя>`
работают. `task_open` — заведение задачи, `task_lifecycle` — claim/update/release,
`task_attested` — закрытие по квитанции, `task_lease` — правила аренды над
строкой, `task_errors` — отказы, `task_store` — SQL.
"""
from __future__ import annotations

from vera_shared.room.task_attested import complete_attested
from vera_shared.room.task_errors import StaleLease, TaskBusy, TaskNotFound
from vera_shared.room.task_lease import (
    HELD_STATUSES,
    RELEASE_STATUSES,
    _require_lease,
    _unconfirmed_voice,
    apply_claim,
    clear_holder_wait,
)
from vera_shared.room.task_lifecycle import claim, release, update
from vera_shared.room.task_open import open_task
from vera_shared.room.task_store import _locked
from vera_shared.room.task_view import task_dict

__all__ = [
    "HELD_STATUSES",
    "RELEASE_STATUSES",
    "StaleLease",
    "TaskBusy",
    "TaskNotFound",
    "_locked",
    "_require_lease",
    "_unconfirmed_voice",
    "apply_claim",
    "claim",
    "clear_holder_wait",
    "complete_attested",
    "open_task",
    "release",
    "task_dict",
    "update",
]
