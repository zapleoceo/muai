"""Исправление искажённых распознаванием имён — чистая функция; стенограмма не меняется.

«Арчевский» в созвоне по работе — это Корчевский. Исправление только по ограниченному
набору кандидатов (участники созвона и сильные контакты владельца) и только когда
уверенно: слово не совпало ни с кем точно, один кандидат заметно ближе остальных.
Результат — связь `name_match` с пониженной уверенностью: читатель видит, что имя
угадано, и может отфильтровать по `min_confidence`.

Сравнение `SequenceMatcher.ratio` — квадратичное, а слов в часовой расшифровке тысячи:
перед ним идут дешёвые отсевы (разница длин, `real_quick_ratio`, `quick_ratio` — верхние
оценки сходства), а вызывающий из цикла событий зовёт `asr_matches_async` (отдельный поток).
"""
from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from dataclasses import dataclass
from difflib import SequenceMatcher

from vera_shared.graph.dupe_keys import name_words, word_key

MIN_RATIO = 0.8
MARGIN = 0.08
MIN_CHARS = 7
MAX_LEN_DIFF = 2
MAX_CONFIDENCE = 0.7
MAX_DISTINCT_WORDS = 3000
_WORD = re.compile(r"[^\W\d_]{%d,}" % MIN_CHARS, re.UNICODE)


@dataclass(frozen=True)
class AsrMatch:
    entity_id: int
    heard: str
    ratio: float

    @property
    def confidence(self) -> float:
        return round(min(MAX_CONFIDENCE, 0.4 + (self.ratio - MIN_RATIO) * 1.5), 2)


def _surname(name: str) -> str | None:
    words = name_words(name)
    return words[-1] if len(words) >= 2 and len(words[-1]) >= MIN_CHARS - 1 else None


def _ratio(key: str, surname: str) -> float:
    """Сходство слова и фамилии; 0 — не стоит считать точно (отсев по верхним оценкам)."""
    if abs(len(key) - len(surname)) > MAX_LEN_DIFF:
        return 0.0
    matcher = SequenceMatcher(None, key, surname)
    if matcher.real_quick_ratio() < MIN_RATIO or matcher.quick_ratio() < MIN_RATIO:
        return 0.0
    return matcher.ratio()


def asr_matches(text: str, candidates: Mapping[int, str]) -> list[AsrMatch]:
    """Слова текста, заметно похожие на фамилию ровно одного кандидата (id → имя)."""
    surnames = {eid: s for eid, name in candidates.items() if (s := _surname(name))}
    exact = set(surnames.values())
    out: dict[tuple[int, str], AsrMatch] = {}
    words = sorted({w for w in _WORD.findall(text) if w[0].isupper()})[:MAX_DISTINCT_WORDS]
    for raw in words:
        key = word_key(raw)
        if any(key == s or key.startswith(s[:-1]) for s in exact):
            continue
        ratios = sorted(((_ratio(key, s), eid) for eid, s in surnames.items()), reverse=True)
        if not ratios or ratios[0][0] < MIN_RATIO:
            continue
        if len(ratios) > 1 and ratios[0][0] - ratios[1][0] < MARGIN:
            continue
        ratio, eid = ratios[0]
        out[(eid, raw)] = AsrMatch(eid, raw, round(ratio, 3))
    return list(out.values())


async def asr_matches_async(text: str, candidates: Mapping[int, str]) -> list[AsrMatch]:
    """То же в отдельном потоке: не держит цикл событий на длинной расшифровке."""
    return await asyncio.to_thread(asr_matches, text, candidates)
