"""HTML письма → текст с сохранением diff-разметки Jira.

Уведомление Jira об изменении описания помечает удалённый текст классом
diff-removed (в старых шаблонах diff-html-removed), добавленный — diff-added,
а заголовок задачи — инлайн-стилем без классов: старое зачёркнуто на фоне
#ffebe6, новое — на фоне #e3fcef. Плоский strip тегов склеивал обе версии, и
пересказ выдавал снятое требование за действующее.

Режим включается только для писем Jira (is_jira_sender): зачёркнутая цена в
рассылке или чужой класс diff-removed письмо не затрагивают.
"""
from __future__ import annotations

import re
from email.utils import parseaddr

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
_REMOVED_ATTR_RE = re.compile(r"diff-(html-)?removed", re.I)
_ADDED_ATTR_RE = re.compile(r"diff-(html-)?added", re.I)
_CHANGED_ATTR_RE = re.compile(r"diff-(html-)?changed", re.I)
_STYLE_REMOVED_RE = re.compile(r"line-through", re.I)
_BG_REMOVED_RE = re.compile(r"background(-color)?\s*:\s*#ffebe6", re.I)
_BG_ADDED_RE = re.compile(r"background(-color)?\s*:\s*#e3fcef", re.I)
_JIRA_DOMAIN_RE = re.compile(r"(^|\.)atlassian\.(net|com)$", re.I)


def is_jira_sender(from_header: str) -> bool:
    """Письмо от jira@*.atlassian.net (или другого адреса домена atlassian)."""
    addr = parseaddr(from_header)[1].lower()
    local, _, domain = addr.rpartition("@")
    return bool(local) and local.startswith("jira") and bool(_JIRA_DOMAIN_RE.search(domain))


def _kind(attrs: str) -> str | None:
    if _REMOVED_ATTR_RE.search(attrs):
        return "removed"
    if _ADDED_ATTR_RE.search(attrs):
        return "added"
    if _STYLE_REMOVED_RE.search(attrs) and _BG_REMOVED_RE.search(attrs):
        return "removed"
    if _BG_ADDED_RE.search(attrs):
        return "added"
    if _CHANGED_ATTR_RE.search(attrs):
        return "changed"
    return None


def has_diff_markup(html: str) -> bool:
    return len(html) <= MAX_DIFF_HTML_CHARS and any(
        m.group(1) == "" and _kind(m.group(3)) for m in _TAG_RE.finditer(html)
    )


def _mark_diff(html: str) -> str:
    """Заменяет diff-элементы маркерами; остальные теги оставляет как есть.

    Висячие закрывающие теги игнорируются, незакрытые маркеры закрываются в
    конце, вложенные маркеры не удваиваются. diff-changed — обёртка над
    изменённым фрагментом: если внутри есть removed/added, она прозрачна, а
    одиночная считается добавленным (текущим) текстом."""
    # (тег, открыл ли маркер, (индекс в out, число маркеров) у diff-changed)
    stack: list[tuple[str, bool, tuple[int, int] | None]] = []
    marked_depth = 0
    marks = 0
    out: list[str] = []
    pos = 0

    def close_top() -> None:
        nonlocal marked_depth
        _, opened, changed = stack.pop()
        if opened:
            marked_depth -= 1
            out.append(f"{_CLOSE} ")
        elif changed and changed[1] == marks:
            out.insert(changed[0], f" {ADDED_OPEN}")
            out.append(f"{_CLOSE} ")

    for m in _TAG_RE.finditer(html):
        out.append(html[pos:m.start()])
        pos = m.end()
        closing, name, attrs = m.group(1), m.group(2).lower(), m.group(3)
        if name in _VOID or attrs.rstrip().endswith("/"):
            out.append(m.group(0))
        elif not closing:
            kind = None if marked_depth else _kind(attrs)
            if kind in ("removed", "added"):
                stack.append((name, True, None))
                marked_depth += 1
                marks += 1
                out.append(f" {REMOVED_OPEN if kind == 'removed' else ADDED_OPEN}")
            else:
                stack.append((name, False,
                              (len(out), marks) if kind == "changed" else None))
                out.append(m.group(0))
        else:
            idx = next((i for i in range(len(stack) - 1, -1, -1)
                        if stack[i][0] == name), None)
            if idx is None:
                continue
            while len(stack) > idx + 1:
                close_top()
            close_top()
            out.append(m.group(0))
    out.append(html[pos:])
    while stack:
        close_top()
    return "".join(out)


def html_to_text(html: str, *, jira: bool = False) -> str:
    """HTML → text. Убирает <script>/<style> ПОЛНОСТЬЮ (содержимое и теги).

    Старый regex `<[^>]+>` оставлял JS-код в тексте — попадал в LLM, тратил
    токены и мог стать prompt injection через email-newsletter с JS внутри.
    jira=True включает пометки [удалено: …] / [добавлено: …].
    """
    html = _SCRIPT_STYLE_RE.sub(" ", html)
    diff = jira and has_diff_markup(html)
    if diff:
        html = _mark_diff(html.replace(_CLOSE, ""))
    text = _ANY_TAG_RE.sub(" ", html)
    text = (text
            .replace("&nbsp;", " ").replace("&amp;", "&")
            .replace("&lt;", "<").replace("&gt;", ">")
            .replace("&quot;", '"').replace("&#39;", "'"))
    text = _WS_RE.sub(" ", text).strip()
    return _SPACE_BEFORE_CLOSE_RE.sub("]", text) if diff else text
