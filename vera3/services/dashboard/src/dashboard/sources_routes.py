"""Источники: `/sources` — один список, `/sources/{key}` — подробности отдельной страницей, `/sources/{key}/panel` — тот же
кусок для спойлера строки.

Ни одного имени источника в этом файле. Список строится из каталога
(`source_registry`) в объединении с тем, что реально лежит в `events`; блоки на
странице источника рисуются из данных, которые вернул провайдер
(`source_detail`). Новый источник добавляет запись в каталог — и появляется
здесь сам.

Так было не всегда: до 2026-08-26 страница набиралась вручную, по блоку HTML на
источник, и Trello, добавленный днём раньше, своего блока так и не получил.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from vera_shared.timeutil import utc_naive_now

from dashboard.render import (
    _render,
    esc,
    local_dt,
    owner_or_blank_401,
    owner_or_redirect,
)
from dashboard.source_freshness import EMPTY, LIVE, NO_POLLING, QUIET, freshness_of
from dashboard.source_panel import panel_html
from dashboard.source_registry import CATALOG, Source, resolve_source
from dashboard.source_state import State, can_disconnect, is_off, state_of
from dashboard.sources_script import SOURCES_SCRIPT
from dashboard.sources_view import source_level
from dashboard.stats import get_source_detail, get_sources_overview
from dashboard.ui.components import collapsible, status_dot

router = APIRouter()


def ago(minutes: int) -> str:
    """«12 мин» / «3 ч» / «78 дн». Минуты для двух месяцев молчания ничего не
    сообщают — «112568 мин» приходилось делить в голове."""
    if minutes < 90:
        return f"{minutes} мин"
    if minutes < 48 * 60:
        return f"{minutes // 60} ч"
    return f"{minutes // 1440} дн"


def _freshness(last: datetime | None, now: datetime, src) -> str:
    """Свежесть потока. Источникам без опроса (внутренние) она не положена."""
    fresh = freshness_of(src, last, now)
    if fresh.state == NO_POLLING:
        return '<span class="mute">—</span>'
    if fresh.state == EMPTY:
        return '<span class="pill err">нет данных</span>'
    if fresh.state == LIVE:
        return f'<span class="pill ok">живой · {ago(fresh.minutes)}</span>'
    if fresh.state == QUIET:
        return f'<span class="pill">тихо · {ago(fresh.minutes)}</span>'
    return f'<span class="pill err">молчит · {ago(fresh.minutes)}</span>'


PROGRESS_BLOCK = (
    '<div id="live-progress" hx-get="/_progress" hx-trigger="load, every 30s" '
    'hx-swap="innerHTML"><div class="muted small">загружается…</div></div>'
)


def _sources_in_order(overview: dict) -> list:
    """Каталог + всё, что есть в events. Источник без записи в каталоге тоже
    показывается: скрыть его — значит соврать про содержимое мозга."""
    known = list(CATALOG)
    extra = sorted(set(overview) - {s.key for s in known})
    return known + [resolve_source(key) for key in extra]


def connection_pill(state: State, src: Source | None = None) -> str:
    """Подключение — не то же, что свежесть потока. Instagram с 353 событиями и
    мёртвой сессией «живым» не является, а только что подключённый Slack ещё
    ничего не принёс и всё равно подключён."""
    if state.connected is None:
        return '<span class="mute">—</span>'
    if src is not None and is_off(src, state):
        return (f'<span class="pill off" title="{esc(state.label)}">'
                f'{esc(src.off_label)}</span>')
    cls = "ok" if state.connected else "err"
    label = state.label or ("подключён" if state.connected else "не подключён")
    return f'<span class="pill {cls}">{esc(label)}</span>'


def actions(src, state: State) -> str:
    """Подключён — «Отключить», нет — «Подключить». Кнопка обязана называть то,
    что произойдёт: «Переподключить» на неподключённом источнике врало."""
    if state.connected and can_disconnect(src.key):
        return (f'<a class="danger" href="/api/sources/{esc(src.key)}/disconnect">'
                f'Отключить</a>')
    if state.connected and src.connect_url:
        return f'<a href="{esc(src.connect_url)}">{esc(src.reconnect_label)}</a>'
    if src.connect_url:
        return (f'<a href="{esc(src.connect_url)}">'
                f'{esc(src.connect_label or "Подключить")}</a>')
    return ""


def _freshness_cell(src: Source, stat: dict, state: State, now: datetime) -> str:
    if is_off(src, state):
        return '<span class="mute">—</span>'
    return _freshness(stat.get("last"), now, src)


def _row(src, stat: dict, state: State, now: datetime) -> str:
    """Строка-спойлер: шапка со сводкой и пустое тело, которое htmx наполнит при раскрытии."""
    total = stat.get("total", 0)
    cls = " idle" if not total else ""
    key = esc(src.key)
    return (
        f'<div class="src-item{cls}" id="src-{key}" data-key="{key}">'
        f'<button type="button" class="src-head" aria-expanded="false" aria-controls="src-body-{key}">'
        f'<span class="chev" aria-hidden="true"></span>'
        f'<span class="src-name">{status_dot(source_level(stat.get("last"), now, src, state))}'
        f'<span class="ico">{src.icon}</span>'
        f'<span class="src-title">{esc(src.title)}<span class="src-how">{esc(src.how)}</span></span></span>'
        f'<span class="src-cell c-conn">{connection_pill(state, src)}</span>'
        f'<span class="src-cell c-fresh">{_freshness_cell(src, stat, state, now)}</span>'
        f'<span class="src-cell num c-total">{total:,}</span>'
        f'<span class="src-cell num c-day">+{stat.get("c24h", 0):,}</span>'
        f'<span class="src-cell c-last">{local_dt(stat.get("last"), "datetime", "—")}</span>'
        f'</button>'
        f'<div class="src-body" id="src-body-{key}" role="region"><div class="src-inner">'
        f'<div class="src-load" hx-get="/sources/{key}/panel" hx-trigger="src-open once" '
        f'hx-swap="innerHTML"><div class="skel-stack"><div class="skeleton"></div>'
        f'<div class="skeleton"></div><div class="skeleton"></div></div></div>'
        f'</div></div></div>'
    )


@router.get("/sources", response_class=HTMLResponse)
async def sources_page(request: Request):
    if (resp := owner_or_redirect(request)) is not None:
        return resp

    now = utc_naive_now()
    overview = await get_sources_overview()
    sources = _sources_in_order(overview)
    states = {s.key: await state_of(s.key) for s in sources}
    rows = "".join(_row(s, overview.get(s.key, {}), states[s.key], now)
                   for s in sources)

    live = sum(1 for st in states.values() if st.connected)
    total_events = sum(v.get("total", 0) for v in overview.values())
    last_24h = sum(v.get("c24h", 0) for v in overview.values())

    return HTMLResponse(_render("sources", f"""
      <div class="head"><h1>Источники</h1></div>
      <p class="note">Всё, откуда Вера берёт события. Нажмите на строку —
         подробности раскроются здесь же. Точка: зелёная — работает, жёлтая — тихо,
         красная — не подключён или давно молчит, серая — выключен, не настроен
         или просто тихо.</p>

      <div class="strip">
        <div><div class="k">Источников</div><div class="v">{len(sources)}</div></div>
        <div><div class="k">Подключено</div><div class="v">{live}</div></div>
        <div><div class="k">Событий всего</div><div class="v">{total_events:,}</div></div>
        <div><div class="k">За сутки</div><div class="v">+{last_24h:,}</div></div>
      </div>

      {collapsible("Конвейер обработки", PROGRESS_BLOCK)}

      <div class="src-list" id="src-list">
        <div class="src-legend" aria-hidden="true"><span></span><span>источник</span><span>подключение</span>
          <span>свежесть</span><span class="num">событий</span><span class="num">за сутки</span><span>последнее</span></div>
        {rows}
      </div>
      <script>{SOURCES_SCRIPT}</script>
    """))


async def _panel_inputs(key: str):
    src = resolve_source(key)
    stat = (await get_sources_overview()).get(key, {})
    return src, stat, await state_of(key), await get_source_detail(key)


@router.get("/sources/{key}/panel", response_class=HTMLResponse)
async def source_panel(key: str, request: Request):
    """Частичный ответ для спойлера: подробности без обвязки страницы."""
    if (resp := owner_or_blank_401(request)) is not None:
        return resp
    src, stat, state, blocks = await _panel_inputs(key)
    return HTMLResponse(panel_html(src, stat, state, blocks))


@router.get("/sources/{key}", response_class=HTMLResponse)
async def source_page(key: str, request: Request):
    if (resp := owner_or_redirect(request)) is not None:
        return resp

    now = utc_naive_now()
    src, stat, state, blocks = await _panel_inputs(key)
    return HTMLResponse(_render("sources", f"""
      <p class="crumb"><a href="/sources#open={esc(key)}">← источники</a></p>
      <div class="head">
        <h1>{src.icon} {esc(src.title)}</h1>
        {connection_pill(state, src)}
        {_freshness_cell(src, stat, state, now)}
      </div>
      {panel_html(src, stat, state, blocks)}
    """))
