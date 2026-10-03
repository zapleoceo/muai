"""Назван ли конец связи в тексте факта — чистые функции без базы.

Сравнение идёт по транслит-ключам слов (`dupe_keys.word_key`), поэтому
«Андрей» в факте находит сущность «Andrey», а падежные окончания («Андрея»,
«Насте») не мешают: у ключа отрезается последняя буква.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from vera_shared.graph.dupe_keys import name_words, word_key

_FIRST_PERSON = re.compile(r"\b(я|мне|мой|моя|моё|мои|меня|мною|i|my|me|saya|aku)\b",
                           re.IGNORECASE)
# «На Андрея (это я)», «Андрей — это я», «I am Andrey»: автор называет себя по имени.
_SELF_AFTER_NAME = re.compile(r"\((?:это\s+)?я\)|\bэто\s+я\b", re.IGNORECASE)
_SELF_BEFORE_NAME = re.compile(r"\bi\s+am\b|\bi['’]m\b", re.IGNORECASE)
_APPOSITION = re.compile(
    r"([\w'’-]+)\s*(?:\((?:это\s+)?я\)|[—–-]\s*это\s+я\b|\bэто\s+я\))", re.IGNORECASE)
_COMPLEMENT = re.compile(r"\bi\s+am\s+([\w'’-]+)|\bi['’]m\s+([\w'’-]+)", re.IGNORECASE)
_POSSESSIVE = re.compile(r"['’]s$", re.IGNORECASE)
# Общие слова названий не доказывают упоминание: «Group», «Inc» есть в любом факте.
_GENERIC = frozenset({"the", "inc", "llc", "ltd", "team", "group", "company", "corp",
                      "and", "for"})
_MIN_STEM = 3


@dataclass(frozen=True)
class End:
    """Один конец связи так, как его видит проверка."""

    names: tuple[str, ...]
    strong: bool = False   # найден по telegram id / email / username, либо это автор
    author: bool = False   # автор сообщения: в факте назван местоимением «я»


@dataclass(frozen=True)
class Evidence:
    fact: str | None
    subject: End
    object: End


def single_token_name(name: str | None) -> bool:
    base = re.sub(r"\([^)]*\)", " ", (name or "").split(" / ", 1)[0])
    return len(re.findall(r"[\w'’-]+", base, re.UNICODE)) <= 1


def _keys(names: Iterable[str]) -> set[str]:
    return {k for name in names for k in name_words(name) if k not in _GENERIC}


def _matches(key: str, word: str) -> bool:
    if len(key) < _MIN_STEM:
        return word == key
    return word.startswith(key[:-1] if len(key) > _MIN_STEM else key)


def mentions(fact: str | None, end: End) -> bool:
    if not fact:
        return False
    if end.author and _FIRST_PERSON.search(fact):
        return True
    words = {word_key(w) for w in re.findall(r"[^\W\d_]{2,}", fact.casefold())}
    return any(_matches(k, w) for k in _keys(end.names) for w in words)


def fact_names_both_ends(evidence: Evidence) -> bool:
    return (mentions(evidence.fact, evidence.subject)
            and mentions(evidence.fact, evidence.object))


def has_self_marker(fact: str | None) -> bool:
    return bool(fact and (_SELF_AFTER_NAME.search(fact) or _SELF_BEFORE_NAME.search(fact)))


def _near(text: str, end: End) -> bool:
    words = {word_key(w) for w in re.findall(r"[^\W\d_]{2,}", text.casefold())}
    return any(_matches(k, w) for k in _keys(end.names) for w in words)


def claims_to_be_author(fact: str | None, end: End) -> bool:
    """Факт сам говорит, что названный конец — автор: имя СРАЗУ перед «(это я)» /
    «— это я» / «это я)» (приложение) либо СРАЗУ после «I am» / «I'm» (сказуемое, не
    притяжательное «Sandra's»). «Позвонил Андрей, это я опоздал» и «I am Anna's friend»
    — не самоназвание."""
    if any(_near(m.group(1), end) for m in _APPOSITION.finditer(fact or "")):
        return True
    return any(_near(token, end)
               for m in _COMPLEMENT.finditer(fact or "")
               if not _POSSESSIVE.search(token := m.group(1) or m.group(2)))
