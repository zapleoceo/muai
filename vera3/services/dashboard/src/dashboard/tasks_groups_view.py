"""Группировка строк `/tasks` по проектам: два блока, в них раскрываемые группы."""
from __future__ import annotations

from collections.abc import Callable

from dashboard.render import esc
from dashboard.tasks_service import Group, TaskItem, group_tasks

GROUP_CSS = """<style>
.tk-block>h2{margin:1.2rem 0 .3rem}.tk-grp{margin:.4rem 0}
.tk-grp>summary{cursor:pointer;font-weight:600;color:var(--text-strong)}
</style>"""


def _group(g: Group, row: Callable[[TaskItem], str]) -> str:
    warn = f" · нужен я: {g.attention}" if g.attention else ""
    return (f'<details class="tk-grp" open><summary>{esc(g.name)} '
            f'<span class="muted">{len(g.items)}{warn}</span></summary>'
            f'{"".join(row(i) for i in g.items)}</details>')


def grouped_rows(items: list[TaskItem], row: Callable[[TaskItem], str]) -> str:
    return "".join(
        f'<section class="tk-block"><h2>{esc(b.title)} <span class="muted">{b.count}</span></h2>'
        f'{"".join(_group(g, row) for g in b.groups)}</section>' for b in group_tasks(items))
