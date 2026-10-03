"""Мелкие повторяемые куски разметки: статусная точка, чип, плитка-число,
сворачиваемый блок. Содержимое-HTML приходит готовым, обычный текст
экранируется здесь."""
from __future__ import annotations

from html import escape

LEVELS = ("ok", "warn", "err")


def _esc(value: str) -> str:
    return escape(value, quote=True)


def status_dot(level: str | None, title: str = "") -> str:
    cls = f"dot {level}" if level in LEVELS else "dot"
    attr = f' title="{_esc(title)}"' if title else ""
    return f'<span class="{cls}"{attr}></span>'


def chip(label: str, href: str | None = None, active: bool = False) -> str:
    on = " on" if active else ""
    if href is None:
        return f'<span class="chip{on}">{_esc(label)}</span>'
    return f'<a class="chip{on}" href="{_esc(href)}">{_esc(label)}</a>'


def stat_card(label: str, value_html: str, sub: str = "") -> str:
    tail = f'<div class="card-sub">{_esc(sub)}</div>' if sub else ""
    return (f'<div class="stat"><div class="k">{_esc(label)}</div>'
            f'<div class="v">{value_html}</div>{tail}</div>')


def collapsible(summary: str, body_html: str, open_: bool = False, dom_id: str = "") -> str:
    attrs = (" open" if open_ else "") + (f' id="{_esc(dom_id)}"' if dom_id else "")
    return f"<details{attrs}><summary>{_esc(summary)}</summary>{body_html}</details>"
