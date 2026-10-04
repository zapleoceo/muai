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


def split_quoted_query(question: str) -> tuple[str, str]:
    """(search text, scope text), using one explicitly requested quotation.

    Multiple quotations keep the existing query semantics: they may represent
    comparisons or alternatives, which this narrow parser cannot distinguish.
    """
    matches = list(_REQUESTED.finditer(question))
    match = matches[0] if len(matches) == 1 else None
    if not matches:
        match = _BARE.fullmatch(question.strip())
        if match:
            focus = next(value for value in match.groupdict().values() if value is not None)
            return (focus.strip(), "") if focus.strip() else (question, question)
    if match is None:
        return question, question
    focus = next(value for value in match.groupdict().values() if value is not None).strip()
    if not focus:
        return question, question
    return focus, question[:match.start()] + " " + question[match.end():]
