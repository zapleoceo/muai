"""Общая оболочка страниц: `<head>` (Pico + слой Веры + htmx), верхнее
меню и подвал. Здесь нет ни `esc`, ни обращений к `render`, чтобы `render`
мог импортировать этот модуль без цикла."""
from __future__ import annotations

from html import escape

from dashboard.ui.favicon import FAVICON_LINKS
from dashboard.ui.theme import HTMX_SRI, HTMX_URL, PICO_SRI, PICO_URL, VERA_CSS
from dashboard.ui.tz import TZ_FOOTER, TZ_SCRIPT

# (ключ страницы, адрес, подпись). Путь «Входящее» и «Люди» остался прежним:
# меняются только подписи.
NAV_ITEMS: tuple[tuple[str, str, str], ...] = (
    ("home", "/", "Поиск"),
    ("events", "/events", "Входящее"),
    ("graph", "/graph", "Люди"),
    ("sources", "/sources", "Источники"),
)
# Страница дублей — часть раздела «Люди».
_NAV_ALIASES = {"entities": "graph"}


def head(title: str) -> str:
    return (
        f'<!DOCTYPE html><html lang="ru" data-theme="dark"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{escape(title)}</title>{FAVICON_LINKS}'
        f'<link rel="stylesheet" href="{PICO_URL}" integrity="{PICO_SRI}" crossorigin="anonymous">'
        f'<script src="{HTMX_URL}" integrity="{HTMX_SRI}" crossorigin="anonymous"></script>'
        f'<style>{VERA_CSS}</style></head>'
    )


def nav(active: str) -> str:
    key = _NAV_ALIASES.get(active, active)

    def link(k: str, href: str, label: str) -> str:
        cur = ' aria-current="page"' if k == key else ""
        return f'<li><a href="{href}"{cur}>{label}</a></li>'

    left = "".join(link(*item) for item in NAV_ITEMS)
    right = (link("settings", "/settings", "⚙")
             + '<li><a class="out" href="/api/logout">выйти</a></li>')
    return (f'<nav class="top"><ul><li><strong>Vera</strong></li>{left}</ul>'
            f'<ul>{right}</ul></nav>')


def page(active: str, body: str) -> str:
    return (head("Vera 3.0") + '<body><main class="container">' + nav(active)
            + body + TZ_FOOTER + "</main>" + TZ_SCRIPT + "</body></html>")


def standalone_html(title: str, body: str) -> str:
    """Страница без меню (вход, подключение источника) в той же оболочке."""
    return (head(title) + '<body><main class="container narrow"><article>'
            + body + "</article></main></body></html>")
