"""Office intake: задачи внешнего источника поверх задач комнаты, права — только на свои.

Проект берётся из принципала, никогда из тела запроса. Ревизия задачи — id последней
строки её журнала (тот же источник, что у MCP Events).

Фасад: создание — `intake_create`, ответ и доставка — `intake_answer`, список —
`intake_list`, ошибки и настройки — `intake_common`. Вызывающие (MCP office_routes,
тесты) обращаются к `intake.<имя>`, поэтому всё реэкспортируется отсюда.
"""
from __future__ import annotations

from vera_shared.office.acl import OfficeForbidden
from vera_shared.office.intake_answer import answer, mark_delivered
from vera_shared.office.intake_common import (
    CLOSED,
    TASK_PREFIX,
    IntakeBusy,
    IntakeConflict,
    IntakeNotFound,
    auto_pickup,
    office_room,
)
from vera_shared.office.intake_create import complete_image_task, create_task
from vera_shared.office.intake_list import _item, list_tasks

__all__ = ["CLOSED", "TASK_PREFIX", "IntakeBusy", "IntakeConflict", "IntakeNotFound",
           "OfficeForbidden", "_item", "answer", "auto_pickup", "complete_image_task",
           "create_task", "list_tasks", "mark_delivered", "office_room"]
