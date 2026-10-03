"""Разметка страницы «Входящее» (`/events`): фильтр, список по дням, «показать
ещё». Запрос в БД остаётся в `events_routes`, здесь только HTML."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any
from urllib.parse import urlencode

from dashboard.event_text import describe, one_line, parse_content
from dashboard.events_filters import source_options, status_options
from dashboard.render import data_table, esc, local_dt
from dashboard.source_registry import resolve_source
from dashboard.ui.components import status_dot

PAGE_STEP = 50
PREVIEW_CHARS = 200

# events.triage_status → (эмодзи, пояснение).
TRIAGE_STATUS_INFO: dict[str, tuple[str, str]] = {
    "done": ("✓", "обработано триажем (важность/проект/темы проставлены)"),
    "pending": ("⏳", "ждёт очереди на обработку триажем"),
    "processing": ("⏳", "обрабатывается прямо сейчас"),
    "error": ("✗", "ошибка при обработке, будет повторная попытка (см. triage_error)"),
    "dead": ("☠", "превышено число попыток — требует ручного разбора"),
    "superseded": ("≈", "заменено похожим более новым событием (семантический дедуп)"),
    "media_pending": ("🖼", "медиа (фото/голос) ждёт vision/распознавания через брокер"),
    "hidden": ("⊘", "скрыто владельцем через MCP (hide_event): не попадает в поиск и выдачи"),
}
STATUS_LABEL: dict[str, str] = {
    "done": "обработано", "pending": "ждёт обработки", "processing": "обрабатывается",
    "error": "ошибка, повторим", "dead": "требует разбора",
    "superseded": "заменено новым", "media_pending": "ждёт распознавания медиа",
    "hidden": "скрыто",
}
_STATUS_DOT = {"done": "ok", "pending": "warn", "processing": "warn",
               "media_pending": "warn", "error": "err", "dead": "err"}

# Подсказки к колонкам технических данных (title=, наведение мышью).
EVENTS_COLUMN_HINTS: dict[str, str] = {
    "id": "Номер события в базе",
    "важность": "Оценка ИИ, 0–100. «—» — ещё не оценено",
    "запрос": "Номер запроса к брокеру — последний вызов ИИ по этому событию",
    "модель": "Какая модель отвечала на этот запрос",
    "токены": "Токены запроса: вход → выход",
    "цена": "Стоимость запроса, USD",
}


def day_label(day: date, today: date) -> str:
    delta = (today - day).days
    if delta == 0:
        return "Сегодня"
    if delta == 1:
        return "Вчера"
    return day.strftime("%d.%m.%Y")


def status_cell(status: str | None) -> str:
    _, desc = TRIAGE_STATUS_INFO.get(status or "", ("?", "неизвестный статус"))
    label = STATUS_LABEL.get(status or "", "неизвестно")
    return status_dot(_STATUS_DOT.get(status or ""), f"{label} — {desc}")


def _tech_cells(e: Mapping[str, Any]) -> str:
    imp = e["importance"] if e["importance"] is not None else "—"
    req = e["request_id"]
    req_cell = f'<span title="{esc(req)}">{esc(req[:8])}…</span>' if req else "—"
    if e["model"] is not None:
        model, tokens = esc(e["model"]), f'{e["tokens_in"]}→{e["tokens_out"]}'
        cost = f'${e["cost_usd"]:.5f}'
    elif e["nature"] is not None and e["has_emb"]:
        model = '<span class="muted" title="групповой вызов — токены учтены в первой строке пачки">в пачке</span>'
        tokens, cost = '<span class="muted">в пачке</span>', "—"
    else:
        model = tokens = cost = "—"
    return (f'<td><a href="/events/{e["id"]}">{e["id"]}</a></td><td>{imp}</td>'
            f'<td class="muted">{req_cell}</td><td>{model}</td>'
            f'<td class="muted">{tokens}</td><td class="muted">{cost}</td>')


def event_row(e: Mapping[str, Any], tech: bool) -> str:
    src = resolve_source(e["source"] or "")
    line = describe(parse_content(e["content_text"]), e.get("metadata"))
    who = esc(line.who or src.title)
    venue = f'<div class="muted small">{esc(line.venue)}</div>' if line.venue else ""
    preview = esc(one_line(line.body or line.subject, PREVIEW_CHARS))
    return (
        f'<tr class="ev row-link" data-href="/events/{e["id"]}" '
        f'data-utc="{esc(e["occurred_at"].isoformat())}Z">'
        f'<td class="muted nowrap">{local_dt(e["occurred_at"], "time")}</td>'
        f'<td title="{esc(src.title)}">{src.icon}</td>'
        f'<td class="who-cell"><div>{who}</div>{venue}</td>'
        f'<td class="preview"><a href="/events/{e["id"]}">{preview or "—"}</a></td>'
        f'<td>{status_cell(e["triage_status"])}</td>{_tech_cells(e) if tech else ""}</tr>'
    )


def day_groups(rows: Iterable[Mapping[str, Any]], today: date, tech: bool) -> str:
    out: list[str] = []
    current: date | None = None
    width = 5 + (6 if tech else 0)
    for e in rows:
        at: datetime = e["occurred_at"]
        if at.date() != current:
            current = at.date()
            out.append(f'<tr class="day-fb"><td colspan="{width}" class="day">'
                       f'{esc(day_label(current, today))}</td></tr>')
        out.append(event_row(e, tech))
    return "".join(out)


def events_table(rows: Iterable[Mapping[str, Any]], today: date, tech: bool) -> str:
    headers = ["время", "", "кто", "текст", ""]
    if tech:
        headers += [f'<span title="{esc(h)}">{c}</span>' for c, h in EVENTS_COLUMN_HINTS.items()]
    return data_table(headers, day_groups(rows, today, tech), empty="ничего не найдено")


def filter_form(sources: list[tuple[str, int]], source: str | None,
                status: str | None, q: str, tech: bool) -> str:
    checked = " checked" if tech else ""
    return f"""
      <form method="get" class="grid">
        <input type="search" name="q" value="{esc(q) if q else ''}" placeholder="Что искать в тексте">
        <select name="source">{source_options(sources, source)}</select>
        <select name="status">{status_options(TRIAGE_STATUS_INFO, status, STATUS_LABEL)}</select>
        <label><input type="checkbox" name="tech" value="1"{checked}> техданные</label>
        <button type="submit">Найти</button>
      </form>"""


def cursor_of(row: Mapping[str, Any]) -> str:
    return f'{row["occurred_at"].isoformat()}_{row["id"]}'


def parse_cursor(raw: str) -> tuple[datetime, int] | None:
    stamp, _, ident = raw.rpartition("_")
    try:
        return datetime.fromisoformat(stamp), int(ident)
    except ValueError:
        return None


def more_link(params: dict[str, Any], last_row: Mapping[str, Any]) -> str:
    qs = urlencode({**{k: v for k, v in params.items() if v}, "before": cursor_of(last_row)})
    return f'<p><a role="button" class="secondary" href="/events?{esc(qs)}">Показать ещё</a></p>'
