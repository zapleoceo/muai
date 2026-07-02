"""REL_EXTRACT_JSON_SCHEMA — structured output вместо json_object.

rel_extract это ~214k запросов/нед (главный объём structured-трафика
Веры), поэтому битый JSON здесь стоит дороже всего. json_schema с
strict=True + predicate-enum прямо в схеме исключает как синтаксически
невалидный JSON, так и семантически невалидный predicate.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from vera_shared.graph.rel_extract import (
    PREDICATES,
    REL_EXTRACT_JSON_SCHEMA,
    extract_and_store,
)


def test_schema_type_is_json_schema_not_json_object():
    assert REL_EXTRACT_JSON_SCHEMA["type"] == "json_schema"


def test_schema_is_strict():
    assert REL_EXTRACT_JSON_SCHEMA["json_schema"]["strict"] is True


def test_schema_predicate_enum_matches_predicates_list():
    """Схема и промпт-текст (который тоже перечисляет PREDICATES) не
    должны разойтись — иначе LLM обучен видеть один список, а схема
    разрешает другой."""
    schema = REL_EXTRACT_JSON_SCHEMA["json_schema"]["schema"]
    item = schema["properties"]["relationships"]["items"]
    assert item["properties"]["predicate"]["enum"] == PREDICATES


def test_schema_relationship_all_fields_required():
    schema = REL_EXTRACT_JSON_SCHEMA["json_schema"]["schema"]
    item = schema["properties"]["relationships"]["items"]
    assert set(item["properties"].keys()) == set(item["required"])


def test_schema_max_3_relationships():
    """Промпт говорит 'максимум 3 связи' — схема должна это же enforce'ить,
    не только полагаться на текстовую инструкцию."""
    schema = REL_EXTRACT_JSON_SCHEMA["json_schema"]["schema"]
    assert schema["properties"]["relationships"]["maxItems"] == 3


def test_schema_confidence_bounded_0_to_1():
    schema = REL_EXTRACT_JSON_SCHEMA["json_schema"]["schema"]
    conf = schema["properties"]["relationships"]["items"]["properties"]["confidence"]
    assert conf["minimum"] == 0.0
    assert conf["maximum"] == 1.0


def test_schema_no_additional_properties():
    schema = REL_EXTRACT_JSON_SCHEMA["json_schema"]["schema"]
    assert schema["additionalProperties"] is False
    item = schema["properties"]["relationships"]["items"]
    assert item["additionalProperties"] is False


def test_schema_is_json_serializable():
    encoded = json.dumps(REL_EXTRACT_JSON_SCHEMA)
    assert json.loads(encoded) == REL_EXTRACT_JSON_SCHEMA


@pytest.mark.asyncio
async def test_extract_and_store_passes_schema_not_json_object():
    """extract_and_store() должен реально передавать REL_EXTRACT_JSON_SCHEMA
    в chat(), не голый json_object."""
    captured = {}

    async def fake_chat(**kwargs):
        captured.update(kwargs)
        return json.dumps({"relationships": []}), {"provider": "test"}

    with patch("vera_shared.graph.rel_extract.chat", AsyncMock(side_effect=fake_chat)):
        n = await extract_and_store(1, "текст события длиннее тридцати символов точно")

    assert n == 0   # пустой relationships список — ничего не вставлено
    assert captured["response_format"] == REL_EXTRACT_JSON_SCHEMA
    assert captured["response_format"]["type"] != "json_object"


@pytest.mark.asyncio
async def test_extract_and_store_skips_short_body_without_calling_chat():
    """Короткий текст (<30 chars) не должен вообще звонить в LLM — no-op guard."""
    with patch("vera_shared.graph.rel_extract.chat", AsyncMock()) as m:
        n = await extract_and_store(1, "коротко")
    assert n == 0
    m.assert_not_called()
