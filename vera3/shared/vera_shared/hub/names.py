"""Имена хаба: проекты, слаги, префиксы инструментов и права агентов.

Инструмент апстрима отдаётся агенту как `<проект>__<апстрим>__<инструмент>`.
Слаги — только `[a-z0-9]`, без `_`, поэтому разбор по первым двум `__`
однозначен, а пара (проект, апстрим) уникальна в БД — коллизий нет.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

PROJECTS: dict[str, str] = {
    "vera": "Vera",
    "aib": "AIbroker",
    "sniffer": "SnifferBot",
    "sim": "SIM",
    "stepan": "Stepan2",
    "itstep": "IT STEP / SintOS",
    "personal": "Personal",
}
SEP = "__"
# MCP ограничивает имя инструмента 128 символами
MAX_TOOL_NAME = 128
_SLUG = re.compile(r"[a-z][a-z0-9]{1,15}")
_AGENT = re.compile(r"[a-z][a-z0-9_-]{1,31}")


class HubNameError(ValueError):
    """Недопустимый слаг, имя агента или запись прав."""


def valid_slug(value: str) -> bool:
    return bool(_SLUG.fullmatch(value or ""))


def valid_agent_name(value: str) -> bool:
    return bool(_AGENT.fullmatch(value or ""))


def tool_name(project: str, upstream: str, tool: str) -> str:
    return f"{project}{SEP}{upstream}{SEP}{tool}"


def split_tool_name(name: str) -> tuple[str, str, str] | None:
    """(проект, апстрим, инструмент) или None, если имя не из хаба."""
    parts = (name or "").split(SEP, 2)
    if len(parts) != 3 or not all(parts):
        return None
    project, upstream, tool = parts
    if project not in PROJECTS or not valid_slug(upstream):
        return None
    return project, upstream, tool


def normalize_allow(entries: Iterable[str]) -> list[str]:
    """Права агента: `проект` (все его апстримы) или `проект/апстрим`. Без «*»:
    доступ ко всему — это явный список всех проектов, а не подстановка."""
    out: set[str] = set()
    for raw in entries:
        entry = (raw or "").strip().lower()
        if not entry:
            continue
        project, _, upstream = entry.partition("/")
        if project not in PROJECTS or (upstream and not valid_slug(upstream)):
            raise HubNameError(f"invalid grant {entry!r}: use project or project/upstream")
        out.add(entry)
    return sorted(out)


def allowed(allow: Iterable[str], project: str, upstream: str) -> bool:
    grants = set(allow)
    return project in grants or f"{project}/{upstream}" in grants
