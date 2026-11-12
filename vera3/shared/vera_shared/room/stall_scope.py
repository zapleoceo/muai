"""Какие задачи не идут на пульт владельца: их ведут в Jira внешние люди.

Решение владельца 12.11.2026: задачи, которые живут в Jira (Lamas, Sintegrum, SinHRM,
ИИ-звонилка и любая задача с Jira-номером или ссылкой в Jira), не показывать на пульте -
их движение зависит от чужой команды, и пульт неделями показывал бы одно и то же.
Список проектов - `STALL_EXCLUDE_PROJECTS` (через запятую, регистр не важен);
пустая строка - не исключать ни одного проекта, признак Jira на задаче работает всегда.
"""
from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from typing import Any

DEFAULT_EXCLUDE_PROJECTS = ("Lamas", "Sintegrum", "SinHRM", "ИИ-звонилка")
_JIRA_KEY = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")
_JIRA_URL = re.compile(r"atlassian\.net|/browse/[A-Z][A-Z0-9]+-\d+")


def excluded_projects(environ: Mapping[str, str] | None = None) -> frozenset[str]:
    raw = (environ if environ is not None else os.environ).get("STALL_EXCLUDE_PROJECTS")
    names = DEFAULT_EXCLUDE_PROJECTS if raw is None else raw.split(",")
    return frozenset(n.strip().casefold() for n in names if n.strip())


def jira_tracked(task_id: str, refs: Iterable[Any] | None) -> bool:
    if _JIRA_KEY.match(task_id or ""):
        return True
    for ref in refs or []:
        if not isinstance(ref, dict):
            continue
        if str(ref.get("kind", "")).casefold() == "jira":
            return True
        if _JIRA_URL.search(str(ref.get("ref", ""))):
            return True
    return False


def off_board(task_id: str, project: str | None, refs: Iterable[Any] | None,
              projects: frozenset[str]) -> bool:
    return (project or "").strip().casefold() in projects or jira_tracked(task_id, refs)
