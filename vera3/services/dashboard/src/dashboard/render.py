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
from dashboard.ui.favicon import FAVICON_LINKS, FAVICON_SVG  # noqa: F401
from dashboard.ui.shell import page, standalone_html


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
    return page(active, body)


_LOGIN_HTML = standalone_html("Vera 3.0 — вход", """
<h1>Vera 3.0</h1>
<p class="muted">Авторизация через Telegram</p>
<div style="display:flex;justify-content:center;margin-top:12px">
<script async src="https://telegram.org/js/telegram-widget.js?22"
        data-telegram-login="__BOT__"
        data-size="large"
        data-radius="10"
        data-auth-url="/api/tg_login"
        data-request-access="write"></script>
</div>""")

_AUTH_ERROR = standalone_html("Доступ запрещён", """
<h1 class="err">Доступ запрещён</h1><p>__MSG__</p>
<p><a href="/login">← вернуться</a></p>""")
