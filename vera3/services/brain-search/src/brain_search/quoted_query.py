"""Separate a requested quotation from surrounding scope and answer instructions."""
from __future__ import annotations

import re

_QUOTE = (
    r'(?:"(?P<plain>[^"\\\r\n]{1,500})"'
    r'|«(?P<angle>[^»\r\n]{1,500})»'
    r'|“(?P<curly>[^”\r\n]{1,500})”)'
)
_REQUESTED = re.compile(r"\b(?:фраз[а-яё]*|цитат[а-яё]*|phrase|quote)\s*:?\s*" + _QUOTE,
                        re.IGNORECASE)
_BARE = re.compile(_QUOTE)
_DELIMITERS = frozenset('"«»“”')
_NEGATIVE = re.compile(
    r"\b(?:no|not|never|without|neither|nor|except|exclud\w*|ignor\w*|"
    r"omit\w*|avoid\w*|lack\w*|missing|absen\w*|\w+n['’]t|"
    r"не|нет|ни|без|кроме|исключ\w*|игнор\w*|отсутств\w*|"
    r"ні|крім|виключ\w*|tidak|tanpa|bukan|kecuali|jangan)\b",
    re.IGNORECASE,
)


def split_quoted_query(question: str) -> tuple[str, str]:
    """(search text, scope text), using one explicitly requested quotation.

    Additional quotation delimiters and negative wording outside the quotation
    keep the existing query semantics. This narrow parser cannot distinguish
    comparisons, alternatives, quoted metadata, or exclusion predicates.
    """
    quotes = list(_BARE.finditer(question))
    if len(quotes) != 1:
        return question, question
    quote = quotes[0]
    outside = question[:quote.start()] + " " + question[quote.end():]
    focus = next(value for value in quote.groupdict().values() if value is not None).strip()
    if (not focus or _DELIMITERS.intersection(outside + focus)
            or _NEGATIVE.search(outside)):
        return question, question
    match = _REQUESTED.search(question)
    if match:
        return focus, question[:match.start()] + " " + question[match.end():]
    if not outside.strip():
        return focus, ""
    return question, question
