"""HTML письма → текст с сохранением diff-разметки Jira.

Уведомление Jira об изменении описания помечает удалённый текст классом
diff-html-removed, добавленный — diff-html-added. Плоский strip тегов склеивал
обе версии, и пересказ выдавал снятое требование за действующее. Режим
включается только по этим классам: зачёркнутая цена в рассылке (<s>, Tailwind
line-through) письмо не затрагивает.
"""
from __future__ import annotations

import re

REMOVED_OPEN = "[удалено: "
ADDED_OPEN = "[добавлено: "
MAX_DIFF_HTML_CHARS = 512 * 1024

_CLOSE = ""  # служебный символ: закрытие только сгенерированного маркера
_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)
_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)([^>]*)>")
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_SPACE_BEFORE_CLOSE_RE = re.compile(r"\s*" + _CLOSE)
_VOID = {"br", "hr", "img", "meta", "link", "input", "wbr"}
_REMOVED_ATTR_RE = re.compile(r"diff-html-removed", re.I)
_ADDED_ATTR_RE = re.compile(r"diff-html-added", re.I)


def _kind(attrs: str) -> str | None:
    if _REMOVED_ATTR_RE.search(attrs):
        return "removed"
    if _ADDED_ATTR_RE.search(attrs):
        return "added"
    return None


def has_diff_markup(html: str) -> bool:
    return len(html) <= MAX_DIFF_HTML_CHARS and any(
        m.group(1) == "" and _kind(m.group(3)) for m in _TAG_RE.finditer(html)
    )


def _mark_diff(html: str) -> str:
    """Заменяет diff-элементы маркерами; остальные теги оставляет как есть.

    Висячие закрывающие теги игнорируются, незакрытые маркеры закрываются в
    конце, вложенные маркеры не удваиваются."""
    stack: list[tuple[str, bool]] = []  # (тег, открыл ли он маркер)
    marked_depth = 0
    out: list[str] = []
    pos = 0
    for m in _TAG_RE.finditer(html):
        out.append(html[pos:m.start()])
        pos = m.end()
        closing, name, attrs = m.group(1), m.group(2).lower(), m.group(3)
        if name in _VOID or attrs.rstrip().endswith("/"):
            out.append(m.group(0))
        elif not closing:
            kind = None if marked_depth else _kind(attrs)
            stack.append((name, kind is not None))
            if kind:
                marked_depth += 1
                out.append(f" {REMOVED_OPEN if kind == 'removed' else ADDED_OPEN}")
            else:
                out.append(m.group(0))
        else:
            idx = next((i for i in range(len(stack) - 1, -1, -1)
                        if stack[i][0] == name), None)
            if idx is None:
                continue
            while len(stack) > idx:
                _, opened = stack.pop()
                if opened:
                    marked_depth -= 1
                    out.append(f"{_CLOSE} ")
            out.append(m.group(0))
    out.append(html[pos:])
    out.extend(f"{_CLOSE} " for _, opened in stack if opened)
    return "".join(out)


def html_to_text(html: str) -> str:
    """HTML → text. Убирает <script>/<style> ПОЛНОСТЬЮ (содержимое и теги).

    Старый regex `<[^>]+>` оставлял JS-код в тексте — попадал в LLM, тратил
    токены и мог стать prompt injection через email-newsletter с JS внутри.
    """
    html = _SCRIPT_STYLE_RE.sub(" ", html)
    diff = has_diff_markup(html)
    if diff:
        html = _mark_diff(html.replace(_CLOSE, ""))
    text = _ANY_TAG_RE.sub(" ", html)
    text = (text
            .replace("&nbsp;", " ").replace("&amp;", "&")
            .replace("&lt;", "<").replace("&gt;", ">")
            .replace("&quot;", '"').replace("&#39;", "'"))
    text = _WS_RE.sub(" ", text).strip()
    return _SPACE_BEFORE_CLOSE_RE.sub("]", text) if diff else text
