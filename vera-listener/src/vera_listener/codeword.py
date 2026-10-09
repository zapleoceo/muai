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

#: Вес обращения в сходстве фразы: дословное «вера» — 1, словоформа из списка
#: («веро», «вэра») — 0.95, усечённое «вер» — 0.9. Усечённое чаще всего
#: оказывается обрывком другого слова, поэтому стоит ниже остальных.
HEAD_VARIANT = 0.95
HEAD_CLIPPED = 0.9

_WORD = re.compile(r"\w+", re.UNICODE)
_NEGATIONS = frozenset({"не", "ни", "нет", "ні"})


@dataclass(frozen=True)
class Hit:
    """Фраза найдена. `instruction` — всё, что сказано после неё (может быть пусто).

    `start` — позиция обращения в реплике (символ), `score` — сходство фразы
    0.8..1, `quoted` — перед фразой слова пересказа («сказал:», «пишет»).
    """

    instruction: str
    start: int = 0
    score: float = 1.0
    quoted: bool = False


def _norm(word: str) -> str:
    return word.lower().replace("ё", "е")


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def words(text: str) -> list[str]:
    """Слова в нижнем регистре, «ё» как «е», без знаков — форма для сравнения."""
    return [_norm(w) for w in _WORD.findall(text)]


def _match_at(words: list[str], start: int, phrase: list[str],
              ) -> tuple[int, float] | None:
    """Совпадает ли фраза с места `start`. → (индекс слова ПОСЛЕ фразы, сходство).

    Длина окна плавает на слово в обе стороны: whisper то склеивает «нужна
    помощь», то разбивает «помощь» пополам.
    """
    if words[start] not in HEAD_FORMS.get(phrase[0], frozenset({phrase[0]})):
        return None
    head = (1.0 if words[start] == phrase[0]
            else HEAD_CLIPPED if words[start] in _CLIPPED_HEADS else HEAD_VARIANT)
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
    return best[1], head * best[0]


#: Усечённые формы обращения — whisper срезает окончание у «Вера» в начале
#: реплики или перед паузой. Посреди фразы «вер» — чаще обрывок другого слова
#: («Серьёзно вер мне…», «это не вер, мне…» — сорванные «верно»/«версия»),
#: поэтому они засчитываются только в начале реплики или перед запятой — и
#: не после отрицания.
_CLIPPED_HEADS = frozenset({"вер"})


def _stands_alone(text: str, spans: list[re.Match[str]], index: int) -> bool:
    if index > 0 and _norm(spans[index - 1].group()) in _NEGATIONS:
        return False
    if index == 0:
        return True
    return text[spans[index].end():].lstrip(" ").startswith(",")


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
        if tokens[start] in _CLIPPED_HEADS and not _stands_alone(text, spans, start):
            continue
        matched = _match_at(tokens, start, target)
        if matched is None:
            continue
        after, score = matched
        while after < len(tokens):
            again = _match_at(tokens, after, target)
            if again is None:
                break
            after = again[0]
        rest = text[spans[after - 1].end():]
        at = spans[start].start()
        return Hit(instruction=rest.strip(" \t\n,.;:!?—-–"), start=at, score=score,
                   quoted=quote_intro(text[:at]))
    return None


#: Пересказ чужих слов: «он сказал: Вера, мне нужна помощь…» — это цитата, а
#: не просьба владельца. Основы глаголов речи; смотрим не дальше трёх слов
#: перед обращением, чтобы «сказал» из прошлой мысли не глушил команду.
_REPORTING = re.compile(
    r"^(сказа|говор|пиш|написа|спроси|спрашива|ответи|отвеча|крича|цитир|"
    r"прочита|читае|произн|повтори|мол$|said|says|wrote|writes|asked)")
_QUOTE_MARKS = ("«", '"', "„", "“")
QUOTE_LOOKBACK = 3


def quote_intro(prefix: str) -> bool:
    """Заканчивается ли `prefix` вводом цитаты: глагол речи или открытая кавычка."""
    tail = prefix.rstrip()
    if tail.endswith(_QUOTE_MARKS):
        return True
    return any(_REPORTING.match(w) for w in words(tail)[-QUOTE_LOOKBACK:])
