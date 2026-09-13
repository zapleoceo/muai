"""Фильтры журнала событий — из данных, а не из списка в разметке.

До 2026-09-13 выпадающий список источников на `/events` был прибит в HTML:
gmail, telegram, instagram, monitor. Слушателя (`voice`), Slack, Trello,
переписки с Claude и памяти агента в нём не было вовсе — события этих
источников в журнале отфильтровать было нельзя, и владелец не мог найти свои
записанные разговоры. Та же грабля, что уже чинили у `/sources`
(см. `source_registry`): список рядом с данными молча отстаёт от данных.

Теперь источники — те, у которых в базе есть события (из общего кэша
статистики, без лишнего скана), подписи — из каталога источников, статусы —
из словаря статусов триажа.
"""
from __future__ import annotations

from collections.abc import Iterable

from dashboard.render import esc
from dashboard.source_registry import resolve_source


def _option(value: str, label: str, selected: bool) -> str:
    mark = " selected" if selected else ""
    return f'<option value="{esc(value)}"{mark}>{esc(label)}</option>'


def source_options(present: Iterable[tuple[str, int]], selected: str | None) -> str:
    """`<option>` на каждый источник, у которого есть события, по убыванию объёма.

    Выбранный источник остаётся в списке, даже если событий у него нет, —
    иначе фильтр сбрасывался бы в «все» и показывал не то, что просили.
    """
    rows = [(key, total) for key, total in present if key]
    if selected and selected not in {key for key, _ in rows}:
        rows.append((selected, 0))
    options = [_option("", "— все источники —", not selected)]
    for key, total in rows:
        src = resolve_source(key)
        count = f"{total:,}".replace(",", " ")      # тысячи пробелом — только в числе
        label = f"{src.icon} {src.title} ({key}) · {count}"
        options.append(_option(key, label, key == selected))
    return "".join(options)


def status_options(statuses: Iterable[str], selected: str | None) -> str:
    options = [_option("", "— любой статус —", not selected)]
    options += [_option(s, s, s == selected) for s in statuses]
    return "".join(options)
