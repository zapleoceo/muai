"""Deterministic validation/normalization of the LLM's triage output —
runs after every triage call regardless of provider, since providers
without constrained-decoding support can still return an out-of-vocab
value."""
from __future__ import annotations

from typing import Any

from vera_shared.projects.rules import VALID_PROJECTS, project_key

# Детерминированная nature по источнику — надёжнее LLM там где источник
# сам по себе определяет природу. Для новых источников решает LLM-поле.
NATURE_BY_SOURCE = {
    "vera_chat": "conversation_with_me",
    # Разговор у ноутбука — всегда разговор со мной, гадать модели не о чем.
    "voice": "conversation_with_me",
    "perplexity": "my_intent",
    "vera_memory": "derived_fact",
}
VALID_NATURES = {"world_event", "my_intent", "conversation_with_me", "derived_fact"}
# Словарь проектов один на всю Веру — vera_shared.projects.rules.
PROJECT_VOCAB = VALID_PROJECTS
# Источники-намерения не эмбеддим: их вектора засоряют семантический поиск
SKIP_EMBED_SOURCES = {"vera_chat", "perplexity"}


def postprocess_triage(parsed: dict[str, Any], source: str) -> dict[str, Any]:
    """Валидация LLM-классификации против словарей + override по source."""
    nature = NATURE_BY_SOURCE.get(source) or str(parsed.get("nature") or "").strip()
    if nature not in VALID_NATURES:
        nature = "world_event"
    parsed["nature"] = nature
    parsed["project"] = project_key(str(parsed.get("project") or "")) or "other"

    # ready_subtype (сделка/Open House для продаж IT STEP Jakarta) больше никто
    # не читает. Схема строгих провайдеров ещё требует поле, поэтому ответ
    # модели здесь выбрасывается: колонка events.ready_subtype остаётся NULL.
    parsed.pop("ready_subtype", None)

    return parsed
