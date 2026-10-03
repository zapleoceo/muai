"""Куски разметки страницы источников: точка состояния и блоки подробностей."""
from __future__ import annotations

from datetime import datetime

from dashboard.render import data_table, esc
from dashboard.source_detail import Block, Html
from dashboard.source_state import State


def source_level(last: datetime | None, now: datetime, src, state: State) -> str | None:
    """Одна точка на источник: красная — не подключён или замолчал, жёлтая —
    тихо, зелёная — живой. Серая — у источника нет понятия «свежесть»."""
    if state.connected is False:
        return "err"
    if src.live_min is None:
        return None
    if last is None:
        return "err"
    mins = max(0, int((now - last).total_seconds() / 60))
    if mins < src.live_min:
        return "ok"
    return "warn" if mins < (src.warn_min or src.live_min * 4) else "err"


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
