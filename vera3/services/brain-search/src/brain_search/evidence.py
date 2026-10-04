"""Bounded source excerpts for answer generation and agent search tools."""
from __future__ import annotations

PRIMARY_CHARS = 4000
SECONDARY_CHARS = 900

EVIDENCE_RULES = (
    "\nEvidence rules: Source excerpts may omit text. Never conclude that a source "
    "contains no reason or detail from an excerpt alone; say what is visible "
    "and what remains unverified. Preserve stated causality and distinguish "
    "account identifiers from payment or transaction identifiers. Do not infer "
    "a reversed cause or relabel an identifier without source evidence.\n"
)


def evidence_excerpt(content: str | None, max_chars: int = PRIMARY_CHARS) -> str:
    """Keep both ends of long events and disclose the missing middle."""
    text = content or ""
    if len(text) <= max_chars:
        return text
    marker = "\n[... middle of stored event omitted; verify full event before claiming absence ...]\n"
    available = max_chars - len(marker)
    head = available * 2 // 3
    return text[:head] + marker + text[-(available - head):]
