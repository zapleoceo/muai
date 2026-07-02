"""TRIAGE_JSON_SCHEMA — structured output вместо json_object.

Регресс-барьер: `response_format={"type": "json_object"}` давал модели
"как получится" — cerebras gpt-oss отдавал битый JSON и терялся (см.
INCIDENT rel_extract). json_schema с strict=True форсит provider-side
grammar-constrained decoding у тех кто её поддерживает (gemini/openai/
groq), так что вывод физически не может нарушить схему.
"""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(__file__), "..", "..",
    "services", "brain-triage", "src",
))

from brain_triage.worker import (  # noqa: E402
    PROJECT_VOCAB,
    TRIAGE_JSON_SCHEMA,
    triage_one,
)


class _FakeEvent:
    id = 1
    source = "telegram"
    account = "demoniwwwe"
    occurred_at = None
    content_text = "тестовое сообщение достаточной длины для триажа"


def test_schema_type_is_json_schema_not_json_object():
    """Регрессия: если кто-то откатит на json_object — тест ловит."""
    assert TRIAGE_JSON_SCHEMA["type"] == "json_schema"


def test_schema_is_strict():
    assert TRIAGE_JSON_SCHEMA["json_schema"]["strict"] is True


def test_schema_has_stable_name():
    assert TRIAGE_JSON_SCHEMA["json_schema"]["name"] == "triage"


def test_schema_project_enum_matches_vocab():
    """enum в схеме и PROJECT_VOCAB (используется в postprocess) не должны
    разойтись — иначе LLM либо не сможет выбрать валидный project, либо
    схема разрешит то что postprocess потом отбросит."""
    schema = TRIAGE_JSON_SCHEMA["json_schema"]["schema"]
    enum = set(schema["properties"]["project"]["enum"])
    assert enum == PROJECT_VOCAB


def test_schema_ready_subtype_allows_null_and_two_values():
    schema = TRIAGE_JSON_SCHEMA["json_schema"]["schema"]
    prop = schema["properties"]["ready_subtype"]
    assert "null" in prop["type"]
    assert set(prop["enum"]) == {"deal", "openhouse", None}


def test_schema_all_top_level_properties_required():
    """strict:true (OpenAI-style) требует что каждое properties-поле
    присутствует в required — иначе некоторые провайдеры отклоняют схему."""
    schema = TRIAGE_JSON_SCHEMA["json_schema"]["schema"]
    assert set(schema["properties"].keys()) == set(schema["required"])


def test_schema_signals_item_all_fields_required():
    schema = TRIAGE_JSON_SCHEMA["json_schema"]["schema"]
    signal_schema = schema["properties"]["signals"]["items"]
    assert set(signal_schema["properties"].keys()) == set(signal_schema["required"])


def test_schema_no_additional_properties_anywhere():
    """Гуляние по вложенным object-схемам: additionalProperties=false
    везде, где type=object — иначе strict-режим у некоторых провайдеров
    (gemini) отклонит схему целиком."""
    schema = TRIAGE_JSON_SCHEMA["json_schema"]["schema"]

    def _check(node):
        if isinstance(node, dict) and node.get("type") == "object":
            assert node.get("additionalProperties") is False, node
        if isinstance(node, dict):
            for v in node.values():
                _check(v)
        elif isinstance(node, list):
            for v in node:
                _check(v)

    _check(schema)


def test_schema_is_json_serializable():
    """httpx.post(json=...) сериализует payload — None внутри enum должен
    остаться валидным JSON null, а не сломать сериализацию."""
    encoded = json.dumps(TRIAGE_JSON_SCHEMA)
    assert json.loads(encoded) == TRIAGE_JSON_SCHEMA


@pytest.mark.asyncio
async def test_triage_one_passes_schema_not_json_object():
    """Живой вызов triage_one() должен реально передать TRIAGE_JSON_SCHEMA
    в chat(), а не голый json_object — иначе константа существует, но
    никуда не подключена."""
    captured = {}

    async def fake_chat(**kwargs):
        captured.update(kwargs)
        return (
            json.dumps({
                "importance": 50, "project": "other", "nature": "world_event",
                "topics": [], "people_mentioned": [], "signals": [],
                "needs_action": False, "ready_subtype": None,
            }),
            {"provider": "test", "model": "test/model"},
        )

    with patch("brain_triage.worker.chat", AsyncMock(side_effect=fake_chat)):
        await triage_one(_FakeEvent())

    assert captured["response_format"] == TRIAGE_JSON_SCHEMA
    assert captured["response_format"]["type"] != "json_object"
