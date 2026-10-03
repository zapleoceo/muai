"""Безопасное подмножество markdown для ответов модели.

Порядок важен: сначала экранируем ВСЁ, и только потом заменяем разметку на
наши собственные теги. Сырой HTML от модели так не доживает до страницы, а
ссылки пропускаются только с http(s) — `javascript:` остаётся текстом.
"""
from __future__ import annotations

import re
from html import escape

_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_BOLD = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_ITALIC_STAR = re.compile(r"(?<![*\w])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![*\w])")
_ITALIC_UNDER = re.compile(r"(?<![_\w])_(?=\S)([^_\n]+?)(?<=\S)_(?![_\w])")
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.*)$")
_HEADING = re.compile(r"^\s*#{1,6}\s+(.*)$")
_STASH = re.compile("\x00(\\d+)\x00")


def _inline(line: str) -> str:
    stash: list[str] = []

    def keep(html: str) -> str:
        stash.append(html)
        return f"\x00{len(stash) - 1}\x00"

    text = _CODE.sub(lambda m: keep(f"<code>{m.group(1)}</code>"), line)
    text = _LINK.sub(lambda m: keep(
        f'<a href="{m.group(2)}" target="_blank" rel="noopener noreferrer">{m.group(1)}</a>'), text)
    text = _BOLD.sub(r"<strong>\2</strong>", text)
    text = _ITALIC_STAR.sub(r"<em>\1</em>", text)
    text = _ITALIC_UNDER.sub(r"<em>\1</em>", text)
    return _STASH.sub(lambda m: stash[int(m.group(1))], text)


def _list(items: list[str]) -> str:
    return "<ul>" + "".join(f"<li>{i}</li>" for i in items) + "</ul>"


def render_markdown(source: str) -> str:
    """Текст модели → безопасный HTML (жирный, курсив, `код`, списки, ссылки)."""
    lines = escape(source.replace("\x00", ""), quote=True).splitlines()
    out: list[str] = []
    items: list[str] = []
    prev_text = False
    gap = False
    for raw in lines:
        if not raw.strip():
            gap = prev_text or gap
            continue
        if (bullet := _BULLET.match(raw)) is not None:
            if not items and prev_text:
                out.append("<br>")
            items.append(_inline(bullet.group(1)))
            prev_text, gap = False, False
            continue
        if items:
            out.append(_list(items))
            items = []
        head = _HEADING.match(raw)
        line = f"<strong>{_inline(head.group(1))}</strong>" if head else _inline(raw)
        if prev_text:
            out.append("<br><br>" if gap else "<br>")
        out.append(line)
        prev_text, gap = True, False
    if items:
        out.append(_list(items))
    return "".join(out)
