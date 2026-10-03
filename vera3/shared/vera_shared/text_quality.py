"""Предикат «у события нет смысла для семантического поиска».

~20% telegram-событий — тела-заглушки («[photo]», «[voice]») или реплики в
пару символов; вектор такого текста — шум, который вытесняет настоящие
совпадения. Событие остаётся в базе, не создаётся только его эмбеддинг.

Правило применяется только к источникам мессенджеров (CHAT_SOURCES) и только
к тексту в их раскладке: строки заголовка («Author: …», «From: …»), первый
разделитель «---», тело. Всё остальное — claude, vera_memory, voice, письма,
текст без такого заголовка — сознательные записи или чужая раскладка, там
«Купил хлеб» не шум; их не трогаем. Разделитель ищется только ПЕРВЫЙ и только
после заголовка: «---» внутри тела (markdown) границей не считается.
"""
from __future__ import annotations

import re

MIN_CONTENT_CHARS = 11
CHAT_SOURCES = frozenset({"telegram", "slack", "instagram"})

_SEPARATOR = "\n---\n"
_HEADER_LINE = re.compile(r"^[A-Za-z][A-Za-z ]{0,20}: ")
_PLACEHOLDER = re.compile(r"\[[A-Za-z_ ]{2,20}\]")


def split_header_body(text: str) -> tuple[str, str] | None:
    """(заголовок, тело) для раскладки «заголовок --- тело», иначе None."""
    head, sep, body = text.partition(_SEPARATOR)
    if not sep or not _HEADER_LINE.match(head.split("\n", 1)[0]):
        return None
    return head, body


def is_contentless(text: str | None, source: str) -> bool:
    if not text or not text.strip():
        return True
    if source not in CHAT_SOURCES:
        return False
    parts = split_header_body(text)
    if parts is None:
        return False
    return len(_PLACEHOLDER.sub("", parts[1]).strip()) < MIN_CONTENT_CHARS
