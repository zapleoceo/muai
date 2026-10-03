"""Структурные сигналы пары для пакета улик — чистые функции по сообщениям."""
from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from vera_shared.graph.pair_roles_lexicon import (
    FORMAL_YOU,
    INFORMAL_YOU,
    INSTRUCTION,
    PATRONYMIC,
    REPORT,
    TITLE,
)
from vera_shared.graph.pair_roles_types import PackMessage

TITLE_EXAMPLES = 3
TITLE_EXAMPLE_CHARS = 90
_LABELS = ("A", "B")


def _count(pattern: re.Pattern[str], messages: Iterable[PackMessage]) -> int:
    return sum(len(pattern.findall(m.text)) for m in messages)


def _style(messages: list[PackMessage], label: str) -> dict[str, int]:
    own = [m for m in messages if m.author == label]
    return {"messages": len(own), "formal_you": _count(FORMAL_YOU, own),
            "informal_you": _count(INFORMAL_YOU, own),
            "name_patronymic": _count(PATRONYMIC, own),
            "gives_instructions": _count(INSTRUCTION, own),
            "reports_or_confirms": _count(REPORT, own)}


def _title_examples(messages: list[PackMessage]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {label: [] for label in _LABELS}
    for m in messages:
        if m.author in out and len(out[m.author]) < TITLE_EXAMPLES and (hit := TITLE.search(m.text)):
            start = max(0, hit.start() - TITLE_EXAMPLE_CHARS // 2)
            out[m.author].append(m.text[start:start + TITLE_EXAMPLE_CHARS].replace("\n", " "))
    return out


def text_signals(messages: list[PackMessage]) -> dict[str, Any]:
    """Слабые эвристики по ВСЕМ сообщениям пары (не только попавшим в пакет): как
    обращаются друг к другу, кто поручает, кто отчитывается, где названа должность.
    Считаются по сообщениям ОТ A и ОТ B друг другу; третьи лица (X) — не в счёт."""
    direct = [m for m in messages if m.author in _LABELS]
    return {"weak_heuristics_per_author": {label: _style(direct, label) for label in _LABELS},
            "title_mentions": _title_examples(messages)}
