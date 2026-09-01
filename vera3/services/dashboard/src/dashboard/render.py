"""Shared HTML template/escape helpers — page chrome, auth-gate shortcuts,
and the small set of HTML fragment builders repeated across every route
module (row list, data table, freshness pill, ETA text).
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from html import escape as _esc

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from dashboard.auth import COOKIE_NAME, require_owner


def esc(v) -> str:
    """HTML-escape для значений из БД/Telegram. Защита от XSS.

    Telethon тащит user-controlled chat_title/sender_username/usernames в БД —
    они идут в рендеринг как-есть. Любой пользователь может назвать чат
    `<script>...</script>` и получить XSS в дашборде.
    """
    if v is None:
        return "—"
    return _esc(str(v), quote=True)


# ─── Local-timezone timestamps ─────────────────────────────────────────────
# Дашборд рендерится на сервере (UTC), но смотрит его Дима из своего часового
# пояса. Вместо strftime на сервере эмитим <time data-utc="...Z"> с UTC-меткой
# и форматируем в браузере под его TZ (см. _TZ_SCRIPT в подвале). Фолбэк-текст
# (UTC) виден если JS выключен. Относительные показы ("N мин назад") НЕ трогаем
# — там разница двух UTC, она одинакова в любом поясе.
_FMT_FALLBACK: dict[str, str] = {
    "datetime": "%Y-%m-%d %H:%M",
    "datetime_sec": "%Y-%m-%d %H:%M:%S",
    "date": "%Y-%m-%d",
    "date_human": "%d %b %Y",
    "time": "%H:%M",
}


def local_dt(dt: datetime | None, fmt: str = "datetime", empty: str = "—") -> str:
    """UTC-метку → `<time>`, который JS переведёт в часовой пояс браузера.

    `fmt` — один из ключей `_FMT_FALLBACK`. `empty` — что показать для None.
    """
    if dt is None:
        return empty
    strf = _FMT_FALLBACK.get(fmt, _FMT_FALLBACK["datetime"])
    iso = dt.isoformat()
    # datetime-колонки в БД — наивный UTC (vera_shared.timeutil.utc_naive_now).
    # Помечаем 'Z', иначе new Date(iso) в браузере распарсит их как ЛОКАЛЬНОЕ
    # время.
    if dt.tzinfo is None:
        iso += "Z"
    return f'<time data-utc="{esc(iso)}" data-fmt="{esc(fmt)}">{dt.strftime(strf)}</time>'


_AVATAR_COLORS = [
    "#4dabf7", "#f783ac", "#69db7c", "#ffa94d", "#9775fa",
    "#3bc9db", "#ffd43b", "#ff8787", "#63e6be", "#b197fc",
]


def initials_avatar_svg(name: str | None, seed: int = 0) -> str:
    """Deterministic initials-on-color-disc SVG — фолбэк, когда реального
    фото профиля нет. Цвет стабилен по seed (entity id), чтобы у одной
    сущности он не прыгал между запросами."""
    name = (name or "?").strip()
    initials = "".join(w[0] for w in name.split()[:2] if w).upper() or "?"
    color = _AVATAR_COLORS[seed % len(_AVATAR_COLORS)]
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        f'<circle cx="32" cy="32" r="32" fill="{color}"/>'
        f'<text x="32" y="41" font-size="26" font-family="sans-serif" '
        f'font-weight="600" fill="#0f1115" text-anchor="middle">{esc(initials)}</text>'
        '</svg>'
    )


def tg_link(username: str | None, tg_id: int | str | None) -> str | None:
    """Ссылка на телеграм-сущность «туда, откуда пришла».

    @username → https://t.me/<username> (открывается и в вебе). Без username —
    tg://user?id=<id> (только внутри приложения Telegram). None если нечем.
    """
    if username:
        return f"https://t.me/{username.lstrip('@')}"
    if tg_id:
        return f"tg://user?id={tg_id}"
    return None


# ─── Auth-gate shortcuts ──────────────────────────────────────────────────
# Every owner-only route repeats `try: require_owner(...) except HTTPException:
# <some failure response>` — only the failure response shape differs. These
# return None on success (caller proceeds) or a ready-to-return response.


def owner_or_redirect(request: Request) -> RedirectResponse | None:
    try:
        require_owner(request, request.cookies.get(COOKIE_NAME))
    except HTTPException:
        return RedirectResponse("/login", status_code=303)
    return None


def owner_or_blank_401(request: Request) -> HTMLResponse | None:
    try:
        require_owner(request, request.cookies.get(COOKIE_NAME))
    except HTTPException:
        return HTMLResponse("", status_code=401)
    return None


def owner_or_auth_error(request: Request) -> HTMLResponse | None:
    try:
        require_owner(request, request.cookies.get(COOKIE_NAME))
    except HTTPException as e:
        return HTMLResponse(
            _AUTH_ERROR.replace("__MSG__", esc(e.detail)).replace("__FAVICON__", FAVICON_LINKS),
            status_code=e.status_code,
        )
    return None


# ─── Small fragment builders (DRY: row list / data table / freshness / ETA) ─


def row_list(pairs: Iterable[tuple[str, str]], empty: str = "—") -> str:
    """`<div class="row">` list — label/value pairs, e.g. per-source counts."""
    html = "".join(
        f'<div class="row"><span>{label}</span><span class="mute">{value}</span></div>'
        for label, value in pairs
    )
    return html or f'<div class="mute">{empty}</div>'


def data_table(headers: list[str], rows_html: str, empty: str = "нет данных") -> str:
    """`<table class="data">` skeleton shared by events/gmail/telegram/instagram tables."""
    thead = "".join(f"<th>{h}</th>" for h in headers)
    tbody = rows_html or f'<tr><td colspan={len(headers)} class="mute">{empty}</td></tr>'
    return (f'<table class="data"><thead><tr>{thead}</tr></thead>'
            f'<tbody>{tbody}</tbody></table>')


def freshness_pill(last_at: datetime | None, now: datetime,
                    live_within_min: int, warn_within_min: int) -> str:
    """'живой/тихо/давно молчит' pill used for Telegram/Instagram/Gmail streams."""
    if last_at is None:
        return '<span class="pill err">нет данных</span>'
    mins = int((now - last_at).total_seconds() / 60)
    if mins < live_within_min:
        return f'<span class="pill ok">живой ({mins} мин назад)</span>'
    if mins < warn_within_min:
        return f'<span class="pill warn">тихо ({mins} мин)</span>'
    return f'<span class="pill err">давно молчит ({mins} мин)</span>'


def format_eta(remaining: int, rate_per_hour: float) -> str:
    """'~N мин/ч/дн' — shared by the home cards and the live-progress fragment."""
    if rate_per_hour <= 0 or remaining <= 0:
        return "—"
    hours = remaining / rate_per_hour
    if hours < 2:
        return f"~{int(hours * 60)} мин"
    if hours < 48:
        return f"~{hours:.1f} ч"
    return f"~{hours / 24:.1f} дн"


# ─── Page chrome ────────────────────────────────────────────────────────────


def _render(active: str, body: str) -> str:
    nav = []
    items = [("home", "/", "главная"),
             ("events", "/events", "log"), ("sources", "/sources", "источники"),
             ("entities", "/entities/duplicates", "сущности"),
             ("graph", "/graph", "граф"),
             ("settings", "/settings", "настройки")]
    for key, href, label in items:
        cls = "active" if active == key else ""
        nav.append(f'<a href="{href}" class="{cls}">{label}</a>')
    nav.append('<a href="/api/logout" style="margin-left:auto;color:#888">выйти</a>')

    return (_HTML_HEAD
            .replace("__FAVICON__", FAVICON_LINKS)
            .replace("__NAV__", "".join(nav))
            + body + _TZ_FOOTER + _TZ_SCRIPT + _HTML_FOOT)


# ─── Favicon (SVG, 32x32 viewBox, scales to 16x16 in tab strips) ────────────
# Visual identity: stylised "V" of two strokes meeting at a bright pulse
# node — events flowing in, settling into memory. Distinct from AIbroker's
# hub-and-spokes icon. Single source of truth: this string is served at
# both /favicon.svg and /favicon.ico, and linked from every HTML page.
FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="6" fill="#0f1115"/>'
    '<line x1="8"  y1="9"  x2="16" y2="22" stroke="#4dabf7" stroke-width="3" stroke-linecap="round"/>'
    '<line x1="24" y1="9"  x2="16" y2="22" stroke="#4dabf7" stroke-width="3" stroke-linecap="round"/>'
    '<circle cx="8"  cy="9"  r="2.5" fill="#4dabf7"/>'
    '<circle cx="24" cy="9"  r="2.5" fill="#4dabf7"/>'
    '<circle cx="16" cy="22" r="3.5" fill="#ffffff"/>'
    '</svg>'
)
FAVICON_LINKS = (
    '<link rel="icon" type="image/svg+xml" href="/favicon.svg">'
    '<link rel="alternate icon" href="/favicon.ico">'
    '<link rel="apple-touch-icon" href="/favicon.svg">'
)


_HTML_HEAD = """<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<title>Vera 3.0</title>__FAVICON__
<script src="https://unpkg.com/htmx.org@1.9.10"></script>
<style>
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       background: #0f1115; color: #e4e6eb; max-width: 1200px;
       margin: 0 auto; padding: 24px; line-height: 1.5; }
h1, h2 { font-weight: 600; margin: 0 0 16px; letter-spacing: -0.01em; }
h2 { font-size: 18px; margin-top: 0; }
a { color: #4dabf7; text-decoration: none; }
nav { display: flex; gap: 6px; margin-bottom: 24px; padding: 6px;
      background: #1a1d24; border-radius: 10px; }
nav a { padding: 8px 14px; border-radius: 6px; color: #aab; }
nav a:hover { background: #2a2d34; color: #e4e6eb; }
nav a.active { background: #2a2d34; color: #fff; font-weight: 600; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
         gap: 14px; margin: 0 0 24px; }
.card { background: #1a1d24; border: 1px solid #2a2d34; border-radius: 12px; padding: 18px; }
.card-label { font-size: 11px; color: #888; text-transform: uppercase;
              letter-spacing: 0.06em; }
.card-value { font-size: 32px; font-weight: 600; margin: 8px 0 4px; }
.card-value small { font-size: 14px; color: #888; font-weight: 400; }
.card-sub { font-size: 12px; color: #888; }
.section { background: #1a1d24; border-radius: 12px; padding: 20px; margin: 16px 0; }
.row { display: flex; justify-content: space-between; padding: 8px 0;
       border-bottom: 1px solid #2a2d34; }
.row:last-child { border-bottom: none; }
.mute { color: #888; }
table.data { width: 100%; border-collapse: collapse; font-size: 13px; }
table.data th, table.data td { padding: 9px 10px; text-align: left;
                                border-bottom: 1px solid #2a2d34; vertical-align: top; }
table.data th { color: #888; font-weight: 500; text-transform: uppercase; font-size: 11px; }
.pill { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11px;
        font-weight: 500; }
.pill.ok { background: #14422c; color: #6dd687; }
.pill.warn { background: #4a3a14; color: #ffc864; }
.pill.err { background: #4a1a1d; color: #ffaaaa; }
.preview { color: #ccc; font-family: 'SF Mono', Monaco, monospace; max-width: 600px;
           overflow: hidden; text-overflow: ellipsis; }
input, select, textarea { padding: 10px 12px; border-radius: 8px; background: #0f1115;
       border: 1px solid #2a2d34; color: #e4e6eb; font-size: 14px; font-family: inherit; }
input[type=text] { width: 100%; padding: 14px; font-size: 15px; }
button { padding: 11px 22px; background: #4dabf7; color: white; border: none;
         border-radius: 8px; font-weight: 600; cursor: pointer; font-size: 14px; }
button:hover { background: #3a9ce0; }
.answer { background: #0f1115; padding: 18px; border-radius: 10px; margin: 14px 0;
          line-height: 1.6; border: 1px solid #2a2d34; }
.meta { color: #888; font-size: 12px; margin-top: 6px; }
.error { background: #4a1a1d; padding: 14px; border-radius: 8px; color: #ffaaaa; }
.htmx-indicator { display: none; }
.htmx-request .htmx-indicator { display: inline; }
.htmx-request.htmx-indicator { display: inline; }
</style></head><body>
<nav>__NAV__</nav>"""

_TZ_FOOTER = (
    '<div id="tz-note" class="mute" '
    'style="margin-top:28px;font-size:11px;text-align:center"></div>'
)

# Переводит все <time data-utc> в часовой пояс браузера. Запускается сразу
# (скрипт в конце body — DOM уже готов) и после каждого htmx-swap (live-прогресс
# подменяется каждые 30с). window.__localizeTimes открыт для ручного вызова.
_TZ_SCRIPT = """<script>
(function(){
  var M=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  function p(n){return String(n).padStart(2,'0');}
  function fmt(d,k){
    var Y=d.getFullYear(),Mo=p(d.getMonth()+1),D=p(d.getDate());
    var h=p(d.getHours()),m=p(d.getMinutes()),s=p(d.getSeconds());
    if(k==='time')return h+':'+m;
    if(k==='date')return Y+'-'+Mo+'-'+D;
    if(k==='date_human')return d.getDate()+' '+M[d.getMonth()]+' '+Y;
    if(k==='datetime_sec')return Y+'-'+Mo+'-'+D+' '+h+':'+m+':'+s;
    return Y+'-'+Mo+'-'+D+' '+h+':'+m;
  }
  function localize(root){
    (root||document).querySelectorAll('time[data-utc]').forEach(function(el){
      var iso=el.getAttribute('data-utc'),d=new Date(iso);
      if(isNaN(d.getTime()))return;
      el.textContent=fmt(d,el.getAttribute('data-fmt')||'datetime');
      el.title='UTC: '+iso;
    });
    var tz=document.getElementById('tz-note');
    if(tz&&!tz.dataset.done){
      // Offset — единственное, что реально определяет показанное время.
      // Название зоны (Asia/Bangkok и т.п.) берётся из настроек ОС/браузера
      // и может отличаться от твоего города при том же offset — города UTC+7
      // (Джакарта, Бангкок, Хошимин) показывают ОДНО И ТО ЖЕ время.
      var off=-new Date().getTimezoneOffset();
      var oh=Math.floor(Math.abs(off)/60),om=Math.abs(off)%60;
      var offStr='UTC'+(off>=0?'+':'-')+oh+(om?':'+p(om):'');
      var zone='';
      try{zone=Intl.DateTimeFormat().resolvedOptions().timeZone;}catch(e){}
      tz.textContent='🕐 время показано в вашем часовом поясе — '+offStr+
        (zone?' (по данным браузера: '+zone+')':'');
      tz.dataset.done='1';
    }
  }
  window.__localizeTimes=localize;
  localize();
  document.body.addEventListener('htmx:afterSwap',function(e){localize(e.target);});
})();
</script>"""

_HTML_FOOT = "</body></html>"


_LOGIN_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Vera 3.0 — вход</title>__FAVICON__
<style>
body { font-family: -apple-system, sans-serif; background: #0f1115; color: #e4e6eb;
       display: flex; align-items: center; justify-content: center; min-height: 100vh;
       margin: 0; }
.box { background: #1a1d24; padding: 48px; border-radius: 16px; max-width: 420px;
       text-align: center; box-shadow: 0 20px 60px rgba(0,0,0,0.5); }
h1 { font-size: 36px; margin: 0 0 8px; }
p { color: #888; margin: 12px 0 28px; }
.tg-widget { display: flex; justify-content: center; margin-top: 12px; }
</style></head><body><div class="box">
<h1>Vera 3.0</h1>
<p>Авторизация через Telegram</p>
<div class="tg-widget">
<script async src="https://telegram.org/js/telegram-widget.js?22"
        data-telegram-login="__BOT__"
        data-size="large"
        data-radius="10"
        data-auth-url="/api/tg_login"
        data-request-access="write"></script>
</div>
</div></body></html>"""


_AUTH_ERROR = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Доступ запрещён</title>__FAVICON__
<style>body{font-family:sans-serif;background:#0f1115;color:#e4e6eb;
       display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
.box{background:#4a1a1d;padding:40px;border-radius:16px;text-align:center;color:#ffaaaa;max-width:400px}
h1{margin:0 0 16px}a{color:#ffaaaa}</style></head>
<body><div class="box"><h1>⛔ Доступ запрещён</h1><p>__MSG__</p>
<p><a href="/login">← вернуться</a></p></div></body></html>"""
