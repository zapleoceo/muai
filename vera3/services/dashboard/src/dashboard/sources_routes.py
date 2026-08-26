"""Источники: `/sources` — один список, `/sources/{key}` — подробности.

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

from dashboard.render import _render, data_table, esc, local_dt, owner_or_redirect
from dashboard.source_detail import Block, Html
from dashboard.source_registry import CATALOG, resolve_source
from dashboard.stats import get_source_detail, get_sources_overview

router = APIRouter()

_STYLE = """<style>
.src-list { width:100%; border-collapse:collapse; font-size:14px; }
.src-list th { font-size:11px; text-transform:uppercase; color:#888; font-weight:500;
               text-align:left; padding:0 12px 8px 0; white-space:nowrap; }
.src-list td { padding:12px 12px 12px 0; border-top:1px solid #2a2d34;
               vertical-align:middle; }
.src-list tr:hover td { background:#171a20; }
.src-name { display:flex; align-items:center; gap:10px; }
.src-name .ico { font-size:17px; width:22px; text-align:center; }
.src-name a { font-weight:600; }
.src-how { color:#6b7280; font-size:12px; margin-top:2px; }
.num { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
.act { text-align:right; white-space:nowrap; }
.act a { font-size:12px; padding:5px 11px; border:1px solid #2a2d34; border-radius:7px;
         color:#9aa4b2; }
.act a:hover { border-color:#4dabf7; color:#4dabf7; }
.idle td { opacity:.55; }
.crumb { font-size:13px; color:#6b7280; margin:0 0 10px; }
.head { display:flex; align-items:baseline; gap:12px; flex-wrap:wrap; margin:0 0 4px; }
.head h1 { margin:0; font-size:24px; }
.strip { display:flex; gap:28px; flex-wrap:wrap; margin:18px 0 4px;
         padding:16px 0; border-top:1px solid #2a2d34; border-bottom:1px solid #2a2d34; }
.strip div { min-width:110px; }
.strip .k { font-size:11px; text-transform:uppercase; letter-spacing:.06em; color:#888; }
.strip .v { font-size:22px; font-weight:600; margin-top:3px;
            font-variant-numeric:tabular-nums; }
.blocks { display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr));
          gap:18px; margin-top:22px; }
.blk { background:#1a1d24; border:1px solid #2a2d34; border-radius:12px; padding:16px 18px; }
.blk.wide { grid-column:1/-1; }
.blk h2 { font-size:13px; text-transform:uppercase; letter-spacing:.06em;
          color:#888; margin:0 0 12px; }
.blk .hint { color:#6b7280; font-size:12px; margin-top:12px; line-height:1.45; }
.note { color:#9aa4b2; font-size:13px; margin:6px 0 0; }
</style>"""


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
    if src.live_min is None:
        return '<span class="mute">—</span>'
    if last is None:
        return '<span class="pill err">нет данных</span>'
    mins = max(0, int((now - last).total_seconds() / 60))
    if mins < src.live_min:
        return f'<span class="pill ok">живой · {ago(mins)}</span>'
    if mins < (src.warn_min or src.live_min * 4):
        return f'<span class="pill warn">тихо · {ago(mins)}</span>'
    return f'<span class="pill err">молчит · {ago(mins)}</span>'


def _sources_in_order(overview: dict) -> list:
    """Каталог + всё, что есть в events. Источник без записи в каталоге тоже
    показывается: скрыть его — значит соврать про содержимое мозга."""
    known = list(CATALOG)
    extra = sorted(set(overview) - {s.key for s in known})
    return known + [resolve_source(key) for key in extra]


def action_label(src, total: int) -> str:
    """Подключён — «Переподключить», пуст — «Подключить». Статичная подпись
    врала бы в одном из состояний."""
    return src.reconnect_label if total else (src.connect_label or "")


def _row(src, stat: dict, now: datetime) -> str:
    total = stat.get("total", 0)
    cls = "" if total else "idle"
    detail = f'<a href="/sources/{esc(src.key)}">{esc(src.title)}</a>' \
        if (src.detail or total) else esc(src.title)
    action = (f'<a href="{esc(src.connect_url)}">{esc(action_label(src, total))}</a>'
              if src.connect_url else "")
    return (
        f'<tr class="{cls}">'
        f'<td><div class="src-name"><span class="ico">{src.icon}</span>'
        f'<span>{detail}<div class="src-how">{esc(src.how)}</div></span></div></td>'
        f'<td>{_freshness(stat.get("last"), now, src)}</td>'
        f'<td class="num">{total:,}</td>'
        f'<td class="num">{stat.get("c24h", 0):,}</td>'
        f'<td>{local_dt(stat.get("last"), "datetime", "—")}</td>'
        f'<td class="act">{action}</td>'
        f'</tr>'
    )


@router.get("/sources", response_class=HTMLResponse)
async def sources_page(request: Request):
    if (resp := owner_or_redirect(request)) is not None:
        return resp

    now = datetime.utcnow()
    overview = await get_sources_overview()
    sources = _sources_in_order(overview)
    rows = "".join(_row(s, overview.get(s.key, {}), now) for s in sources)

    live = sum(1 for s in sources if s.live_min is not None and s.connect_url)
    total_events = sum(v.get("total", 0) for v in overview.values())
    last_24h = sum(v.get("c24h", 0) for v in overview.values())

    return HTMLResponse(_render("sources", f"""
      {_STYLE}
      <div class="head"><h1>Источники</h1></div>
      <p class="note">Всё, откуда Вера берёт события. Имя источника —
         ссылка на подробности.</p>

      <div class="strip">
        <div><div class="k">Источников</div><div class="v">{len(sources)}</div></div>
        <div><div class="k">Подключаемых</div><div class="v">{live}</div></div>
        <div><div class="k">Событий всего</div><div class="v">{total_events:,}</div></div>
        <div><div class="k">За сутки</div><div class="v">+{last_24h:,}</div></div>
      </div>

      <table class="src-list">
        <thead><tr>
          <th>источник</th><th>поток</th><th class="num">событий</th>
          <th class="num">за сутки</th><th>последнее</th><th></th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
    """))


def _cell(value) -> str:
    return value if isinstance(value, Html) else esc(value)


def _render_block(b: Block) -> str:
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
    rows = "".join("<tr>" + "".join(f"<td>{_cell(c)}</td>" for c in r) + "</tr>"
                   for r in b["rows"])
    wide = " wide" if len(b["headers"]) > 3 else ""
    table = data_table(b["headers"], rows, b.get("empty", "нет данных"))
    return f'<div class="blk{wide}">{title}{table}{hint}</div>'


@router.get("/sources/{key}", response_class=HTMLResponse)
async def source_page(key: str, request: Request):
    if (resp := owner_or_redirect(request)) is not None:
        return resp

    now = datetime.utcnow()
    src = resolve_source(key)
    stat = (await get_sources_overview()).get(key, {})
    blocks = await get_source_detail(key)

    action = (f'<a href="{esc(src.connect_url)}" '
              f'style="padding:8px 16px;border:1px solid #4dabf7;border-radius:8px">'
              f'{esc(action_label(src, stat.get("total", 0)))}</a>'
              if src.connect_url else "")
    note = f'<p class="note">{esc(src.note)}</p>' if src.note else ""
    body = "".join(_render_block(b) for b in blocks) or \
        '<div class="blk"><div class="mute">Разбивок для этого источника нет — ' \
        'он не хранит своего состояния.</div></div>'

    return HTMLResponse(_render("sources", f"""
      {_STYLE}
      <p class="crumb"><a href="/sources">← источники</a></p>
      <div class="head">
        <h1>{src.icon} {esc(src.title)}</h1>
        {_freshness(stat.get("last"), now, src)}
        <span style="margin-left:auto">{action}</span>
      </div>
      <p class="note">{esc(src.how)}</p>
      {note}

      <div class="strip">
        <div><div class="k">Событий</div>
             <div class="v">{stat.get("total", 0):,}</div></div>
        <div><div class="k">За час</div>
             <div class="v">+{stat.get("c1h", 0):,}</div></div>
        <div><div class="k">За сутки</div>
             <div class="v">+{stat.get("c24h", 0):,}</div></div>
        <div><div class="k">Последнее</div>
             <div class="v" style="font-size:15px">
               {local_dt(stat.get("last"), "datetime_sec", "—")}</div></div>
      </div>

      <div class="blocks">{body}</div>
    """))
