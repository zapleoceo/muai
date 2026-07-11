"""Deterministic validation/normalization of the LLM's triage output —
runs after every triage call regardless of provider, since providers
without constrained-decoding support can still return an out-of-vocab
value."""
from __future__ import annotations

from typing import Any

# Детерминированная nature по источнику — надёжнее LLM там где источник
# сам по себе определяет природу. Для новых источников решает LLM-поле.
NATURE_BY_SOURCE = {
    "vera_chat": "conversation_with_me",
    "perplexity": "my_intent",
    "vera_memory": "derived_fact",
}
VALID_NATURES = {"world_event", "my_intent", "conversation_with_me", "derived_fact"}
PROJECT_VOCAB = {"itstep", "veranda", "family", "personal", "news", "other"}
# Источники-намерения не эмбеддим: их вектора засоряют семантический поиск
SKIP_EMBED_SOURCES = {"vera_chat", "perplexity"}


def postprocess_triage(parsed: dict[str, Any], source: str) -> dict[str, Any]:
    """Валидация LLM-классификации против словарей + override по source."""
    nature = NATURE_BY_SOURCE.get(source) or str(parsed.get("nature") or "").strip()
    if nature not in VALID_NATURES:
        nature = "world_event"
    project = str(parsed.get("project") or "").lower().strip()
    if project not in PROJECT_VOCAB:
        project = "other"
    parsed["nature"] = nature
    parsed["project"] = project

    # Валидация ready_subtype
    ready_subtype = parsed.get("ready_subtype")
    if isinstance(ready_subtype, str):
        ready_subtype = ready_subtype.strip().lower()
    if ready_subtype not in (None, "deal", "openhouse"):
        ready_subtype = None
    # Enforce: ready_subtype can only be set if needs_action=true
    if not parsed.get("needs_action"):
        ready_subtype = None
    parsed["ready_subtype"] = ready_subtype

    return parsed
