"""rel_extract contract with the model: predicates, prompt, strict JSON schema,
response parsing and the per-run outcome."""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field

# Первое лицо в тексте («я работаю в X») — это АВТОР сообщения, не сущность
# с именем «Я». Раньше «Я» резолвилось по имени и все такие связи падали на
# случайный аккаунт, чей first_name = «Я» (найдено вживую: 221 ребро от 6+
# разных авторов на одном чужом человеке).
SELF_TOKENS = {"я", "i", "me", "myself"}

PREDICATES = [
    "boss_of",          # X is boss of Y
    "reports_to",       # X reports to Y (inverse of boss_of, model picks one)
    "coworker_of",      # X and Y work together
    "co_founder_of",    # X is co-founder of org Y
    "works_at",         # X works at org Y
    "client_of",        # X is client of Y (org or person)
    "vendor_of",        # X provides services/goods to Y
    "spouse_of",        # symmetric
    "parent_of",        # X is parent of Y
    "child_of",         # inverse
    "friend_of",        # symmetric
    "lives_in",         # X lives in place Y
]

PROMPT = """Извлеки факты-связи между сущностями из текста сообщения.
Возвращай ТОЛЬКО JSON по схеме — без префиксов, без markdown.

Доступные предикаты: {preds}

Правила:
  - Только связи которые ЯВНО упомянуты в тексте, не выводи из контекста
  - Если subject/object — сам автор сообщения («я», «мне», от первого лица),
    пиши РОВНО "Я" — система сама подставит автора
  - Если нет уверенных связей — верни {{"relationships": []}}
  - Максимум 3 связи на одно сообщение

Schema:
{{
  "relationships": [
    {{"subject": "<name>", "predicate": "<one of above>",
      "object": "<name>", "fact": "<verbatim text justifying it>",
      "confidence": <0.0-1.0>}}
  ]
}}

Текст:
{body}"""

# json_schema вместо json_object — форсит grammar-constrained decoding у
# провайдеров которые его поддерживают (gemini/openai/groq), так что
# битый JSON (частая причина потерь на cerebras gpt-oss) физически не
# может быть сгенерирован. predicate ограничен PREDICATES прямо в схеме —
# не полагаемся только на промпт-инструкцию.
REL_EXTRACT_JSON_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "rel_extract",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "relationships": {
                    "type": "array",
                    "maxItems": 3,
                    "items": {
                        "type": "object",
                        "properties": {
                            "subject": {"type": "string"},
                            "predicate": {"type": "string", "enum": PREDICATES},
                            "object": {"type": "string"},
                            "fact": {"type": "string"},
                            "confidence": {"type": "number", "minimum": 0.0,
                                           "maximum": 1.0},
                        },
                        "required": [
                            "subject", "predicate", "object", "fact", "confidence",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["relationships"],
            "additionalProperties": False,
        },
    },
}


@dataclass
class RelExtractOutcome:
    """Итог одного прогона — чтобы нулевой выход был виден в логе, а не в DEBUG."""

    llm_failed: bool = False
    returned: int = 0
    unresolved: int = 0
    rejected: Counter[str] = field(default_factory=Counter)
    inserted: int = 0


def parse_relationships(raw: str) -> list[dict] | None:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    rels = data.get("relationships") if isinstance(data, dict) else None
    if not isinstance(rels, list):
        return None
    return [r for r in rels[:3] if isinstance(r, dict)]

