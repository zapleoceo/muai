"""Bounded source excerpts for answer generation and agent search tools."""
from __future__ import annotations

import re
from collections.abc import Sequence

from brain_search.identifiers import exact_identifiers
from brain_search.lang import content_words

#: Письмо типичной длины (4-5 тыс. знаков) должно влезать целиком в топ-3.
PRIMARY_CHARS = 8000
SECONDARY_CHARS = 900

EVIDENCE_RULES = (
    "\nEvidence rules: Source excerpts may omit text. Never conclude that a source "
    "contains no reason or detail from an excerpt alone; say what is visible "
    "and what remains unverified. Preserve stated causality and distinguish "
    "account identifiers from payment or transaction identifiers. Do not infer "
    "a reversed cause or relabel an identifier without source evidence. "
    "Events close in time do not establish a causal link. Do not present a "
    "login or security alert as the cause of a payment or advertising "
    "restriction unless a source explicitly connects them. When asked for a "
    "stated reason, give that reason without unsolicited speculative causes, "
    "even if other events are cited. If asked for hypotheses, separate them "
    "from the stated reason, label them unverified, and name the missing "
    "evidence. Keep an email's stated local date and an "
    "event's UTC timestamp distinct; label the time zone when dates differ. "
    "Search returns a limited selection, not a complete inventory. A missing result means "
    "only that it was not returned by this search, never that the event does "
    "not exist in the source. Check an explicitly named event ID directly. "
    "If an excerpt shows the omission marker and the asked detail is not "
    "visible, say that part of the source was not shown and the detail is "
    "unverified; never say it is \"not mentioned\" or «не указано».\n"
)

#: Room #3920: "PHP 8.5.12 ... fix two critical vulnerabilities" was answered as "affected".
POLARITY_RULES = (
    "\nPolarity rules: \"Version X fixes/addresses ...\" and \"fixed/patched/"
    "resolved in X\" mean X contains the fix and is NOT affected; \"a flaw/"
    "vulnerability in X\" and \"X is vulnerable/affected\" mean X is affected. "
    "Earlier versions are affected only if the source says so. Never list fixed "
    "versions as affected. State affected versions or exposure only when a "
    "source says it explicitly, otherwise say the source does not specify. For "
    "any claim about a vulnerability's affected/fixed versions or security "
    "status, quote the exact source phrase in quotes next to its [event:ID].\n"
)


MIN_TERM_LEN = 5
MAX_TERMS = 12
#: Ниже этого бюджета окно не помещается осмысленно: остаётся голова+хвост.
MIN_WINDOW_BUDGET = 300
OMISSION_MARKER = (
    "\n[... middle of stored event omitted: part of the event is not shown "
    "here; verify full event before claiming absence ...]\n")


def excerpt_terms(question: str) -> list[str]:
    """Идентификаторы и характерные слова вопроса: что искать в пропущенной середине.

    Токенайзер и стоп-слова общие с поиском (lang.py), своего не заводим."""
    words = [w for w in content_words(question) if len(w) >= MIN_TERM_LEN]
    return list(dict.fromkeys(exact_identifiers(question) + words))[:MAX_TERMS]


def _first_match(text: str, terms: Sequence[str], lo: int, hi: int) -> int | None:
    """Самая ранняя позиция любого термина в text[lo:hi], без учёта регистра."""
    found = [m.start() for t in terms if t
             for m in [re.compile(re.escape(t), re.IGNORECASE).search(text, lo, hi)] if m]
    return min(found) if found else None


def evidence_excerpt(content: str | None, max_chars: int = PRIMARY_CHARS,
                     terms: Sequence[str] = ()) -> str:
    """Голова и хвост длинного события; середина раскрывается, если там термин запроса.

    Без терминов или без совпадения в пропущенной части результат прежний."""
    text = content or ""
    if len(text) <= max_chars:
        return text
    marker = OMISSION_MARKER
    available = max_chars - len(marker)
    head = available * 2 // 3
    tail = available - head
    pos = _first_match(text, terms, head, len(text) - tail) if terms else None
    if pos is not None and max_chars - 2 * len(marker) >= MIN_WINDOW_BUDGET:
        budget = max_chars - 2 * len(marker)
        head = tail = budget // 3
        window = budget - head - tail
        start = max(head, min(pos - window // 4, len(text) - tail - window))
        return (text[:head] + marker + text[start:start + window] + marker
                + text[-tail:])
    return text[:head] + marker + text[-tail:]
