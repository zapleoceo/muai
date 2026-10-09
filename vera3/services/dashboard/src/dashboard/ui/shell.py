"""Общая оболочка страниц: `<head>` (наш CSS + htmx), верхнее меню и подвал.
Здесь нет ни `esc`, ни обращений к `render`, чтобы `render` мог импортировать
этот модуль без цикла."""
from __future__ import annotations

from html import escape

from dashboard.ui.favicon import FAVICON_LINKS
from dashboard.ui.theme import CSS_URL, HTMX_SRI, HTMX_URL, JS_URL
from dashboard.ui.tz import DAYS_SCRIPT, TZ_FOOTER, TZ_SCRIPT

# (ключ страницы, адрес, подпись). Путь «Входящее» и «Люди» остался прежним:
# меняются только подписи.
NAV_ITEMS: tuple[tuple[str, str, str], ...] = (
    ("home", "/", "Поиск"),
    ("events", "/events", "Входящее"),
    ("graph", "/graph", "Люди"),
    ("sources", "/sources", "Источники"),
    ("tasks", "/tasks", "Задачи"),
)
# Страница дублей — часть раздела «Люди».
_NAV_ALIASES = {"entities": "graph"}

_GEAR = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
         'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
         '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1'
         'a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3'
         'l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1'
         ' 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1'
         'a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21'
         'a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg>')


def head(title: str) -> str:
    return (
        f'<!DOCTYPE html><html lang="ru" data-theme="dark"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<meta name="color-scheme" content="dark">'
        f'<title>{escape(title)}</title>{FAVICON_LINKS}'
        f'<link rel="stylesheet" href="{CSS_URL}">'
        f'<script src="{HTMX_URL}" integrity="{HTMX_SRI}" crossorigin="anonymous"></script>'
        f'<script src="{JS_URL}" defer></script></head>'
    )


def nav(active: str) -> str:
    key = _NAV_ALIASES.get(active, active)

    def link(k: str, href: str, label: str, title: str = "") -> str:
        cur = ' aria-current="page"' if k == key else ""
        hint = f' title="{escape(title)}" aria-label="{escape(title)}"' if title else ""
        return f'<li><a href="{href}"{cur}{hint}>{label}</a></li>'

    left = "".join(link(*item) for item in NAV_ITEMS)
    on = ' class="on"' if key == "settings" else ""
    menu = (f'<li><details class="menu"><summary{on} title="Настройки и выход" aria-label="Меню">{_GEAR}</summary>'
            '<div class="menu-pop"><a class="only-mobile" href="/journal">Журнал правок</a><a href="/settings">Настройки</a>'
            '<a class="out" href="/api/logout">Выйти</a></div></details></li>')
    right = link("journal", "/journal", "Журнал").replace("<li>", '<li class="nav-journal">', 1) + menu
    return (f'<nav class="top" aria-label="Разделы"><ul><li class="brand"><span class="orb"></span>'
            f'<span class="word">Vera</span></li>{left}</ul><ul>{right}</ul></nav>')


def page(active: str, body: str, wide: bool = False) -> str:
    cls = "container wide" if wide else "container"
    return (head("Vera 3.0") + f'<body><main class="{cls}">' + nav(active)
            + body + TZ_FOOTER + "</main>" + TZ_SCRIPT + DAYS_SCRIPT + "</body></html>")


def standalone_html(title: str, body: str) -> str:
    """Страница без меню (вход, подключение источника) в той же оболочке."""
    return (head(title) + '<body><main class="container narrow"><article>'
            + body + "</article></main></body></html>")
