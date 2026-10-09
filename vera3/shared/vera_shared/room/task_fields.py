"""Проверка полей очереди задачи: project, depends_on."""
from __future__ import annotations

import re
from typing import Any

from vera_shared.room.task_refs import validate_refs

MAX_DEPENDS_ON = 20
PROJECT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def validate_project(project: str) -> str:
    if not isinstance(project, str) or not PROJECT_RE.match(project):
        raise ValueError("project must match [A-Za-z0-9_.-]{1,64}")
    return project


def validate_depends_on(task_id: str, depends_on: list[str]) -> list[str]:
    if not isinstance(depends_on, list) or len(depends_on) > MAX_DEPENDS_ON:
        raise ValueError(f"depends_on must be a list of at most {MAX_DEPENDS_ON} task_ids")
    for dep in depends_on:
        if not isinstance(dep, str) or not 1 <= len(dep) <= 128:
            raise ValueError("depends_on items must be task_id strings of 1..128 chars")
    if task_id in depends_on:
        raise ValueError("a task cannot depend on itself")
    if len(set(depends_on)) != len(depends_on):
        raise ValueError("depends_on must not contain duplicates")
    return list(depends_on)


def validate_open_fields(task_id: str, *, project: str | None, priority: int | None,
                         auto_pickup: bool | None, depends_on: list[str] | None,
                         next_action: str | None, refs: list[dict[str, Any]] | None,
                         ) -> dict[str, Any]:
    """Поля очереди при создании задачи: только заданные, проверки те же, что в update."""
    if priority is not None and not 0 <= priority <= 3:
        raise ValueError("priority must be 0..3 (0 most urgent, default 2)")
    if next_action is not None and len(next_action) > 2000:
        raise ValueError("next_action must be at most 2000 chars")
    checked = {
        "project": validate_project(project) if project is not None else None,
        "priority": priority, "auto_pickup": auto_pickup, "next_action": next_action,
        "depends_on": (validate_depends_on(task_id, depends_on)
                       if depends_on is not None else None),
        "refs": validate_refs(refs) if refs is not None else None}
    return {k: v for k, v in checked.items() if v is not None}
