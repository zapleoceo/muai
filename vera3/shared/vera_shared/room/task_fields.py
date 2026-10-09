"""Проверка полей очереди задачи: project, depends_on."""
from __future__ import annotations

import re

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
