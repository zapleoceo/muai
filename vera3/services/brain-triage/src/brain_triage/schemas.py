"""json_schema (strict) definitions for single-event and batch triage calls.

json_schema (не json_object) — провайдеры с grammar-constrained decoding
(gemini, openai, groq) физически не могут выдать невалидный JSON или
значение вне enum. json_object давал модели "как получится" — часть
ответов (особенно cerebras gpt-oss) приходила битой и терялась.
postprocess_triage() остаётся: providers без constrained-decoding
(litellm's drop_params тихо роняет response_format) всё ещё нуждаются
в client-side защите.
"""
from __future__ import annotations

from typing import Any

from brain_triage.config import TRIAGE_GROUP_BATCH_SIZE
from brain_triage.postprocess import PROJECT_VOCAB

# _TRIAGE_ITEM_PROPERTIES — общий "один результат триажа" переиспользуется в
# одиночной схеме (TRIAGE_JSON_SCHEMA) и в батч-схеме (TRIAGE_BATCH_JSON_SCHEMA,
# где это ITEM внутри массива "results" + event_id). Не дублируем руками —
# batch-схема добавляет event_id к тем же полям.
_TRIAGE_ITEM_PROPERTIES: dict[str, Any] = {
    "importance": {"type": "integer", "minimum": 0, "maximum": 100},
    "project": {"type": "string", "enum": sorted(PROJECT_VOCAB)},
    "nature": {"type": "string", "enum": ["world_event", "my_intent"]},
    "topics": {"type": "array", "items": {"type": "string"}},
    "people_mentioned": {"type": "array", "items": {"type": "string"}},
    "signals": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["task", "event", "news", "offer",
                             "question", "decision", "anomaly"],
                },
                "summary": {"type": "string"},
                "date": {"type": ["string", "null"]},
            },
            "required": ["type", "summary", "date"],
            "additionalProperties": False,
        },
    },
    "needs_action": {"type": "boolean"},
    "ready_subtype": {
        "type": ["string", "null"],
        "enum": ["deal", "openhouse", None],
    },
}
_TRIAGE_ITEM_REQUIRED = [
    "importance", "project", "nature", "topics",
    "people_mentioned", "signals", "needs_action", "ready_subtype",
]

TRIAGE_JSON_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "triage",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": _TRIAGE_ITEM_PROPERTIES,
            "required": _TRIAGE_ITEM_REQUIRED,
            "additionalProperties": False,
        },
    },
}

# Батч-версия: массив результатов, каждый привязан к event_id, чтобы ответ
# разложить обратно по событиям (порядок ответа LLM не гарантирован).
TRIAGE_BATCH_JSON_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "triage_batch",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "results": {
                    "type": "array",
                    "maxItems": TRIAGE_GROUP_BATCH_SIZE,
                    "items": {
                        "type": "object",
                        "properties": {
                            "event_id": {"type": "integer"},
                            **_TRIAGE_ITEM_PROPERTIES,
                        },
                        "required": ["event_id", *_TRIAGE_ITEM_REQUIRED],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["results"],
            "additionalProperties": False,
        },
    },
}
