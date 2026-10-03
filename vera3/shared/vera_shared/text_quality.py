"""Предикат «у события нет смысла для семантического поиска».

~20% telegram-событий — тела-заглушки («[photo]», «[voice]») или реплики в
пару символов; вектор такого текста — шум, который вытесняет настоящие
совпадения. Событие остаётся в базе, не создаётся только его эмбеддинг.

content_text: строки заголовка («Author: …», «From: …», «Chat: …»), затем
разделитель «---» и тело. Судим тело; у писем смысл ещё и в «Subject:».
"""
from __future__ import annotations

import re

MIN_CONTENT_CHARS = 11

_SEPARATOR = "\n---\n"
_PLACEHOLDER = re.compile(r"\[[A-Za-z_ ]{2,20}\]")
_SUBJECT = re.compile(r"^Subject:[ \t]*(.*)$", re.MULTILINE)


def split_header_body(text: str) -> tuple[str, str]:
    head, sep, body = text.partition(_SEPARATOR)
    return (head, body) if sep else ("", text)


def _meaningful_len(fragment: str) -> int:
    return len(_PLACEHOLDER.sub("", fragment).strip())


def is_contentless(text: str | None) -> bool:
    if not text or not text.strip():
        return True
    head, body = split_header_body(text)
    if _meaningful_len(body) >= MIN_CONTENT_CHARS:
        return False
    subject = _SUBJECT.search(head)
    return not (subject and _meaningful_len(subject.group(1)) >= MIN_CONTENT_CHARS)
