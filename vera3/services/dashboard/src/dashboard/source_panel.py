"""Подробности источника одним куском разметки.

Один и тот же кусок показывают спойлер строки на `/sources` (подгружается
htmx с `/sources/{key}/panel` при первом раскрытии) и отдельная страница
`/sources/{key}` для прямых ссылок. Функции чистые: данные приносит маршрут.
"""
from __future__ import annotations

from dashboard.render import esc, local_dt
from dashboard.source_registry import Source
from dashboard.source_state import State, can_disconnect
from dashboard.sources_view import render_block

NO_BLOCKS = ('<div class="blk"><div class="mute">Разбивок для этого источника нет — '
             'он не хранит своего состояния.</div></div>')


def disconnect_form(src: Source, state: State) -> str:
    """Отключение через общий диалог подтверждения (`data-confirm`), а не страницу-вопрос."""
    question = (f"Отключить «{src.title}»? {state.affects or 'Приём событий остановится'}. "
                "Уже собранные события останутся, секрет из базы не удаляется — шаг обратим.")
    return (f'<form class="inline" method="post" action="/api/sources/{esc(src.key)}/disconnect" '
            f'data-confirm="{esc(question)}"><button type="submit" class="danger">Отключить</button></form>')


def panel_actions(src: Source, state: State) -> str:
    """Подключить / переподключить (ссылка на флоу источника) и отключить."""
    parts = []
    if src.connect_url:
        label = src.reconnect_label if state.connected else (src.connect_label or "Подключить")
        parts.append(f'<a class="btn" href="{esc(src.connect_url)}">{esc(label)}</a>')
    if state.connected and can_disconnect(src.key):
        parts.append(disconnect_form(src, state))
    return " ".join(parts)


def _strip(stat: dict) -> str:
    cells = [("Событий", f'{stat.get("total", 0):,}', ""),
             ("За час", f'+{stat.get("c1h", 0):,}', ""),
             ("За сутки", f'+{stat.get("c24h", 0):,}', ""),
             ("Последнее", local_dt(stat.get("last"), "datetime_sec", "—"), " v-small")]
    return '<div class="strip">' + "".join(
        f'<div><div class="k">{k}</div><div class="v{cls}">{v}</div></div>'
        for k, v, cls in cells) + "</div>"


def panel_html(src: Source, stat: dict, state: State, blocks: list, *,
               actions: bool = True) -> str:
    note = f'<p class="note">{esc(src.note)}</p>' if src.note else ""
    body = "".join(render_block(b) for b in blocks) or NO_BLOCKS
    bar = f'<div class="src-actions">{panel_actions(src, state)}</div>' if actions else ""
    return (f'<p class="note">{esc(src.how)}</p>{note}{bar}{_strip(stat)}'
            f'<div class="blocks">{body}</div>')

