"""HTML письма → текст с сохранением diff-разметки Jira.

Уведомление Jira об изменении описания показывает удалённый текст зачёркнутым,
добавленный — подчёркнутым. Плоский strip тегов склеивал обе версии в одну,
и пересказ выдавал снятое требование за действующее.
"""
from __future__ import annotations

import re

REMOVED_OPEN = "[удалено: "
ADDED_OPEN = "[добавлено: "
_MARK_CLOSE = "]"

_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)
_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)([^>]*)>")
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_SPACE_BEFORE_CLOSE_RE = re.compile(r"\s+\]")
_VOID = {"br", "hr", "img", "meta", "link", "input", "wbr"}
_REMOVED_TAGS = {"del", "s", "strike"}
_REMOVED_ATTR_RE = re.compile(r"diff-html-removed|diff-removed|line-through", re.I)
_ADDED_ATTR_RE = re.compile(r"diff-html-added|diff-added", re.I)


def _kind(name: str, attrs: str) -> str | None:
    if name in _REMOVED_TAGS or _REMOVED_ATTR_RE.search(attrs):
        return "removed"
    if name == "ins" or _ADDED_ATTR_RE.search(attrs):
        return "added"
    return None


def has_diff_markup(html: str) -> bool:
    return any(
        m.group(1) == "" and _kind(m.group(2).lower(), m.group(3))
        for m in _TAG_RE.finditer(html)
    )


def _mark_diff(html: str) -> str:
    """Заменяет diff-элементы маркерами; остальные теги оставляет как есть."""
    stack: list[tuple[str, str | None]] = []
    out: list[str] = []
    pos = 0
    for m in _TAG_RE.finditer(html):
        out.append(html[pos:m.start()])
        pos = m.end()
        closing, name, attrs = m.group(1), m.group(2).lower(), m.group(3)
        if name in _VOID or attrs.rstrip().endswith("/"):
            out.append(m.group(0))
        elif not closing:
            kind = _kind(name, attrs)
            stack.append((name, kind))
            out.append({"removed": f" {REMOVED_OPEN}", "added": f" {ADDED_OPEN}"}
                       .get(kind or "", m.group(0)))
        else:
            while stack and stack[-1][0] != name:
                stack.pop()
            kind = stack.pop()[1] if stack else None
            out.append(f"{_MARK_CLOSE} " if kind else m.group(0))
    out.append(html[pos:])
    return "".join(out)


def html_to_text(html: str) -> str:
    """HTML → text. Убирает <script>/<style> ПОЛНОСТЬЮ (содержимое и теги).

    Старый regex `<[^>]+>` оставлял JS-код в тексте — попадал в LLM, тратил
    токены и мог стать prompt injection через email-newsletter с JS внутри.
    """
    html = _SCRIPT_STYLE_RE.sub(" ", html)
    if has_diff_markup(html):
        html = _mark_diff(html)
    text = _ANY_TAG_RE.sub(" ", html)
    text = (text
            .replace("&nbsp;", " ").replace("&amp;", "&")
            .replace("&lt;", "<").replace("&gt;", ">")
            .replace("&quot;", '"').replace("&#39;", "'"))
    return _SPACE_BEFORE_CLOSE_RE.sub("]", _WS_RE.sub(" ", text).strip())
