"""Кодовая фраза в распознанной реплике: «Вера, мне нужна помощь, <поручение>».

Сравнение нестрогое, потому что whisper пишет фразу по-разному: «Веро»,
«помошь», без запятых, с заглавной или без. Строгое равенство пропускало бы
живые команды, а допуск без якоря ловил бы обычную речь. Поэтому обращение
(первое слово) сверяется по явному списку словоформ, а остаток фразы —
нестрого, целиком.

Обращение по списку, а не по сходству: нечёткое сравнение пропускало «Я верю,
что мне нужна помощь» («верю» ~ «вера» = 0.75) — обычную фразу разговора.
Похожих на «вера» слов в русском много (верю, верно, верить, мера), а
команда — действие, поэтому здесь белый список того, что whisper реально
пишет вместо «Вера».
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

DEFAULT_PHRASE = "Вера, мне нужна помощь"

#: Словоформы обращения, которые принимаются за «Вера». Для своей кодовой
#: фразы (`VERA_CODEWORD`) обращение — её первое слово дословно.
HEAD_FORMS: dict[str, frozenset[str]] = {
    "вера": frozenset({"вера", "веро", "веру", "вер", "вэра"}),
}

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
    if words[start] not in HEAD_FORMS.get(phrase[0], frozenset({phrase[0]})):
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
