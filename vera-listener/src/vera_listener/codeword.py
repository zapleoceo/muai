"""Кодовая фраза в распознанной реплике: «Вера, мне нужна помощь, <поручение>».

Сравнение нестрогое, потому что whisper пишет фразу по-разному: «Веро»,
«помошь», без запятых, с заглавной или без. Строгое равенство пропускало бы
живые команды, а допуск без якоря ловил бы обычную речь. Поэтому два порога:
первое слово (обращение) сверяется отдельно, остаток фразы — целиком.

Обращение сверяется строже остального сознательно: «мне нужна помощь» без
«Вера» — обычная фраза разговора, и без отдельной проверки обращения она
проходила бы общий порог с любым словом перед собой («очень мне нужна помощь»
даёт 0.81 по всей фразе — ровно на пороге).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

DEFAULT_PHRASE = "Вера, мне нужна помощь"

#: Порог для обращения. «Веро» и «Веру» дают 0.75, «верно» и «ведро» — 0.67,
#: «вечно» — 0.44: подобрано тестами на искажениях (`test_codeword.py`), не на
#: глаз. Плюс первая буква обязана совпасть: «Мера» тоже даёт 0.75, а окончание
#: whisper путает гораздо чаще, чем начало слова.
HEAD_RATIO = 0.74

#: Порог для остатка фразы. «мне нужно помошь» против «мне нужна помощь» —
#: 0.87, «мне нужна помощь» без одного слова («мне помощь») — 0.67.
TAIL_RATIO = 0.8

_WORD = re.compile(r"\w+", re.UNICODE)
_NEGATIONS = frozenset({"не", "ни", "нет", "ні"})


@dataclass(frozen=True)
class Hit:
    """Фраза найдена. `instruction` — всё, что сказано после неё (может быть пусто)."""

    instruction: str


def _norm(word: str) -> str:
    return word.lower().replace("ё", "е")


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def words(text: str) -> list[str]:
    """Слова в нижнем регистре, «ё» как «е», без знаков — форма для сравнения."""
    return [_norm(w) for w in _WORD.findall(text)]


def _match_at(words: list[str], start: int, phrase: list[str]) -> int | None:
    """Совпадает ли фраза с места `start`. → индекс слова ПОСЛЕ фразы.

    Длина окна плавает на слово в обе стороны: whisper то склеивает «нужна
    помощь», то разбивает «помощь» пополам.
    """
    head = words[start]
    if head[:1] != phrase[0][:1] or _ratio(head, phrase[0]) < HEAD_RATIO:
        return None
    tail = " ".join(phrase[1:])
    best: tuple[float, int] | None = None
    for size in (len(phrase) - 2, len(phrase) - 1, len(phrase)):
        end = start + 1 + size
        if size < 1 or end > len(words):
            continue
        window = words[start + 1:end]
        # «Вера, мне НЕ нужна помощь» проходит порог (0.91), но значит обратное.
        # Отрицание, которого нет в самой фразе, отменяет совпадение.
        if any(w in _NEGATIONS and w not in phrase for w in window):
            continue
        score = _ratio(" ".join(window), tail)
        if best is None or score > best[0]:
            best = (score, end)
    if best is None or best[0] < TAIL_RATIO:
        return None
    return best[1]


def find(text: str, phrase: str = DEFAULT_PHRASE) -> Hit | None:
    """Первое вхождение фразы в реплике и поручение после него.

    Повтор фразы сразу за ней («Вера, мне нужна помощь, Вера, мне нужна
    помощь, напиши…») срезается: иначе в поручение попало бы само обращение.
    """
    target = words(phrase)
    if len(target) < 2:
        return None
    spans = list(_WORD.finditer(text))
    tokens = [_norm(m.group()) for m in spans]
    for start in range(len(tokens)):
        after = _match_at(tokens, start, target)
        if after is None:
            continue
        while after < len(tokens):
            again = _match_at(tokens, after, target)
            if again is None:
                break
            after = again
        rest = text[spans[after - 1].end():]
        return Hit(instruction=rest.strip(" \t\n,.;:!?—-–"))
    return None
