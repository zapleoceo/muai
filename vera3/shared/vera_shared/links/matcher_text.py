"""Текстовые помощники сопоставления имён: слова, падежные окончания, окно рядом, начало предложения."""
from __future__ import annotations

import re

from vera_shared.graph.dupe_keys import word_key

#: Слова полного имени должны стоять рядом: «Иван ... Петров» через абзац — не имя.
NAME_WINDOW = 5
WORD = re.compile(r"[^\W\d_]{2,}", re.UNICODE)
USERNAME = re.compile(r"@([A-Za-z0-9_]{4,32})")
#: Перед словом стоит это (или начало текста) — слово открывает предложение: заглавная буква
#: там ничего не говорит о собственном имени.
_SENTENCE_END = frozenset(".!?…:;\n—–-\"«(“")


def name_keys(name: str) -> list[str]:
    return [word_key(w) for w in WORD.findall(name)]


def same_word(key: str, word: str) -> bool:
    """Тот же слово с точностью до падежного окончания."""
    if word == key:
        return True
    return len(key) > 3 and word.startswith(key[:-1])


def text_words(text: str) -> list[tuple[str, str]]:
    return [(word_key(w), w) for w in WORD.findall(text)]


def sentence_initial(text: str, offset: int) -> bool:
    before = text[:offset].rstrip(" \t")
    return not before or before[-1] in _SENTENCE_END


def find_word(words: list[tuple[str, str]], key: str) -> list[int]:
    return [i for i, (k, _) in enumerate(words) if same_word(key, k)]


def near(positions: list[list[int]]) -> bool:
    """Есть ли выбор по одной позиции из каждого списка, где соседние слова в окне."""
    def chain(last: int, rest: list[list[int]]) -> bool:
        return not rest or any(abs(p - last) <= NAME_WINDOW and chain(p, rest[1:])
                               for p in rest[0])
    return any(chain(p, positions[1:]) for p in positions[0])
