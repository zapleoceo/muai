"""Куски разметки страницы источников: точка состояния и блоки подробностей."""
from __future__ import annotations

from datetime import datetime

from dashboard.render import data_table, esc
from dashboard.source_detail import Block, Html
from dashboard.source_freshness import EMPTY, LIVE, QUIET, SILENT, freshness_of
from dashboard.source_registry import Source
from dashboard.source_state import State


def is_off(src: Source, state: State) -> bool:
    """Необязательный источник, который владелец не включал."""
    return src.optional and state.connected is False


def source_level(last: datetime | None, now: datetime, src: Source, state: State) -> str | None:
    """Одна точка на источник: красная — не подключён или замолчал, зелёная —
    живой. Серая — у источника нет «свежести», он выключен по выбору или
    просто тихий (ночь и выходные — не тревога)."""
    if is_off(src, state):
        return None
    if state.connected is False:
        return "err"
    fresh = freshness_of(src, last, now).state
    return {LIVE: "ok", SILENT: "err", EMPTY: "err", QUIET: None}.get(fresh)


def cell(value) -> str:
    return value if isinstance(value, Html) else esc(value)


def render_block(b: Block) -> str:
    hint = f'<div class="hint">{esc(b["hint"])}</div>' if b.get("hint") else ""
    title = f'<h2>{esc(b["title"])}</h2>' if b.get("title") else ""
    if b["kind"] == "rows":
        body = "".join(
            f'<div class="row"><span>{esc(k)}</span>'
            f'<span class="mute">{esc(v)}</span></div>'
            for k, v in b["pairs"]
        ) or '<div class="mute">нет данных</div>'
        return f'<div class="blk">{title}{body}{hint}</div>'

    # По умолчанию экранируем всё; разметку провайдер помечает типом Html.
    # Обратное правило («провайдер сам не забудет esc») дало бы XSS на первом
    # же чате с названием <script>…</script> — они приходят из БД как есть.
    rows = "".join("<tr>" + "".join(f"<td>{cell(c)}</td>" for c in r) + "</tr>"
                   for r in b["rows"])
    wide = " wide" if len(b["headers"]) > 3 else ""
    table = data_table(b["headers"], rows, b.get("empty", "нет данных"))
    return f'<div class="blk{wide}">{title}{table}{hint}</div>'
