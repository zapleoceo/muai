"""Групповой батчинг триажа — chat_kind классификация, чанкинг,
батч-промпт, батч-схема, triage_group_batch() парсинг ответа LLM."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(__file__), "..", "..",
    "services", "brain-triage", "src",
))

from brain_triage.worker import (  # noqa: E402
    TRIAGE_BATCH_JSON_SCHEMA,
    TRIAGE_GROUP_BATCH_MAX_CHARS,
    TRIAGE_GROUP_BATCH_SIZE,
    _chunk_group_rows,
    build_batch_prompt,
    chat_kind,
    triage_group_batch,
)
from vera_shared.db.models import EventRow


def _row(id_=1, content="привет", metadata=None, source="telegram") -> EventRow:
    return EventRow(
        id=id_, source=source, source_event_id=f"tg:{id_}",
        account="userbot", content_text=content,
        occurred_at=datetime(2026, 7, 2), metadata_=metadata or {},
    )


# ─── chat_kind ───────────────────────────────────────────────────────────


class TestChatKindNewField:
    """Новые события (после фикса userbot.py) несут явный chat_kind."""

    def test_explicit_private(self):
        assert chat_kind(_row(metadata={"chat_kind": "private"})) == "private"

    def test_explicit_group(self):
        assert chat_kind(_row(metadata={"chat_kind": "group"})) == "group"

    def test_explicit_channel(self):
        assert chat_kind(_row(metadata={"chat_kind": "channel"})) == "channel"

    def test_explicit_wins_over_legacy_fields(self):
        # chat_kind присутствует — legacy is_supergroup/chat_type игнорируются
        row = _row(metadata={"chat_kind": "channel", "chat_type": "chat"})
        assert chat_kind(row) == "channel"

    def test_explicit_empty_string_falls_to_other(self):
        assert chat_kind(_row(metadata={"chat_kind": ""})) == "other"


class TestChatKindLegacyFallback:
    """Backlog записан ДО фикса — только chat_type/is_supergroup, без chat_kind."""

    def test_legacy_user_is_private(self):
        assert chat_kind(_row(metadata={"chat_type": "user"})) == "private"

    def test_legacy_chat_is_group(self):
        assert chat_kind(_row(metadata={"chat_type": "chat"})) == "group"

    def test_legacy_chatfull_is_group(self):
        assert chat_kind(_row(metadata={"chat_type": "chatfull"})) == "group"

    def test_legacy_supergroup_channel_is_group(self):
        """Регресс-тест бага: Telethon отдаёт супергруппы КАК Channel —
        без is_supergroup=True это ошибочно классифицировалось бы как
        вещательный канал."""
        row = _row(metadata={"chat_type": "channel", "is_supergroup": True})
        assert chat_kind(row) == "group"

    def test_legacy_broadcast_channel_not_supergroup(self):
        row = _row(metadata={"chat_type": "channel", "is_supergroup": False})
        assert chat_kind(row) == "channel"

    def test_legacy_channel_missing_is_supergroup_defaults_broadcast(self):
        assert chat_kind(_row(metadata={"chat_type": "channel"})) == "channel"

    def test_unknown_chat_type_is_other(self):
        assert chat_kind(_row(metadata={"chat_type": "bot"})) == "other"

    def test_no_metadata_at_all_is_other(self):
        assert chat_kind(_row(metadata=None)) == "other"
        assert chat_kind(_row(metadata={})) == "other"


# ─── _chunk_group_rows ──────────────────────────────────────────────────


class TestChunkGroupRows:
    def test_empty_input(self):
        assert _chunk_group_rows([]) == []

    def test_fewer_than_batch_size_one_chunk(self):
        rows = [_row(i) for i in range(3)]
        chunks = _chunk_group_rows(rows)
        assert len(chunks) == 1
        assert len(chunks[0]) == 3

    def test_splits_at_max_batch_size(self):
        rows = [_row(i, content="x") for i in range(25)]
        chunks = _chunk_group_rows(rows, max_batch=10, max_chars=999999)
        assert [len(c) for c in chunks] == [10, 10, 5]

    def test_splits_at_max_chars_even_under_batch_size(self):
        """Один аномально длинный текст обрывает chunk раньше max_batch."""
        rows = [
            _row(1, content="a" * 5000),
            _row(2, content="b" * 5000),
            _row(3, content="c" * 100),
        ]
        chunks = _chunk_group_rows(rows, max_batch=10, max_chars=6000)
        # первый чанк: [1] (5000), добавить 2 -> 10000 > 6000 -> новый chunk
        assert len(chunks) >= 2
        assert chunks[0] == [rows[0]]

    def test_single_oversized_row_gets_its_own_chunk(self):
        """Один элемент ВСЕГДА попадает в текущий (даже пустой) chunk —
        не теряется, даже если сам по себе превышает max_chars."""
        rows = [_row(1, content="x" * 100000)]
        chunks = _chunk_group_rows(rows, max_batch=10, max_chars=6000)
        assert chunks == [[rows[0]]]

    def test_default_constants_match_module_config(self):
        assert TRIAGE_GROUP_BATCH_SIZE >= 1
        assert TRIAGE_GROUP_BATCH_MAX_CHARS > 0


# ─── build_batch_prompt ─────────────────────────────────────────────────


class TestBuildBatchPrompt:
    def test_includes_all_event_ids(self):
        rows = [_row(1), _row(2), _row(3)]
        prompt = build_batch_prompt(rows)
        for r in rows:
            assert f"[event_id={r.id}]" in prompt

    def test_includes_event_count(self):
        rows = [_row(1), _row(2)]
        prompt = build_batch_prompt(rows)
        assert "2 коротких сообщений" in prompt

    def test_includes_content(self):
        rows = [_row(1, content="УНИКАЛЬНЫЙ_ТЕКСТ_42")]
        assert "УНИКАЛЬНЫЙ_ТЕКСТ_42" in build_batch_prompt(rows)

    def test_truncates_long_content_per_event(self):
        rows = [_row(1, content="x" * 5000)]
        prompt = build_batch_prompt(rows)
        # per-event cap 2000 chars — не весь 5000-символьный текст
        assert prompt.count("x") < 5000


# ─── TRIAGE_BATCH_JSON_SCHEMA structure ─────────────────────────────────


class TestBatchSchema:
    def test_type_is_json_schema(self):
        assert TRIAGE_BATCH_JSON_SCHEMA["type"] == "json_schema"

    def test_strict(self):
        assert TRIAGE_BATCH_JSON_SCHEMA["json_schema"]["strict"] is True

    def test_results_array_has_event_id_plus_triage_fields(self):
        item = TRIAGE_BATCH_JSON_SCHEMA["json_schema"]["schema"] \
            ["properties"]["results"]["items"]
        assert "event_id" in item["properties"]
        assert "importance" in item["properties"]
        assert "event_id" in item["required"]
        assert set(item["properties"].keys()) == set(item["required"])

    def test_max_items_matches_group_batch_size(self):
        schema = TRIAGE_BATCH_JSON_SCHEMA["json_schema"]["schema"]
        assert schema["properties"]["results"]["maxItems"] == TRIAGE_GROUP_BATCH_SIZE

    def test_is_json_serializable(self):
        encoded = json.dumps(TRIAGE_BATCH_JSON_SCHEMA)
        assert json.loads(encoded) == TRIAGE_BATCH_JSON_SCHEMA


# ─── triage_group_batch ──────────────────────────────────────────────────


def _triage_item(event_id: int, **overrides) -> dict:
    base = {
        "event_id": event_id, "importance": 40, "project": "other",
        "nature": "world_event", "topics": [], "people_mentioned": [],
        "signals": [], "needs_action": False, "ready_subtype": None,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_group_batch_happy_path_all_events_returned():
    rows = [_row(1), _row(2), _row(3)]

    async def fake_chat(**kwargs):
        return (
            json.dumps({"results": [_triage_item(r.id) for r in rows]}),
            {"provider": "mistral", "model": "mistral/small"},
        )

    with patch("brain_triage.worker.chat", AsyncMock(side_effect=fake_chat)):
        out = await triage_group_batch(rows)

    assert set(out.keys()) == {1, 2, 3}
    for eid, meta in out.items():
        assert meta is not None
        assert meta["triaged_by_provider"] == "mistral"
        assert "event_id" not in meta   # stripped before storage


@pytest.mark.asyncio
async def test_group_batch_partial_response_missing_event_is_none():
    """LLM обрезала ответ и вернула только 2 из 3 — третье None, не теряется
    (вызывающий код вернёт его в pending на одиночный ретрай)."""
    rows = [_row(1), _row(2), _row(3)]

    async def fake_chat(**kwargs):
        return (
            json.dumps({"results": [_triage_item(1), _triage_item(2)]}),
            {"provider": "mistral"},
        )

    with patch("brain_triage.worker.chat", AsyncMock(side_effect=fake_chat)):
        out = await triage_group_batch(rows)

    assert out[1] is not None
    assert out[2] is not None
    assert out[3] is None


@pytest.mark.asyncio
async def test_group_batch_ignores_hallucinated_event_id():
    """LLM выдумала event_id, которого не было в запросе — игнорируем эту
    запись, не роняем весь batch."""
    rows = [_row(1)]

    async def fake_chat(**kwargs):
        return (
            json.dumps({"results": [
                _triage_item(1), _triage_item(999),  # 999 не наш
            ]}),
            {"provider": "mistral"},
        )

    with patch("brain_triage.worker.chat", AsyncMock(side_effect=fake_chat)):
        out = await triage_group_batch(rows)

    assert set(out.keys()) == {1}
    assert out[1] is not None


@pytest.mark.asyncio
async def test_group_batch_empty_rows_returns_empty_without_calling_chat():
    with patch("brain_triage.worker.chat", AsyncMock()) as m:
        out = await triage_group_batch([])
    assert out == {}
    m.assert_not_called()


@pytest.mark.asyncio
async def test_group_batch_malformed_top_level_raises():
    rows = [_row(1)]

    async def fake_chat(**kwargs):
        return json.dumps({"not_results": []}), {"provider": "x"}

    with patch("brain_triage.worker.chat", AsyncMock(side_effect=fake_chat)):
        with pytest.raises(ValueError, match="results"):
            await triage_group_batch(rows)


@pytest.mark.asyncio
async def test_group_batch_strips_markdown_fences():
    rows = [_row(1)]

    async def fake_chat(**kwargs):
        payload = json.dumps({"results": [_triage_item(1)]})
        return f"```json\n{payload}\n```", {"provider": "x"}

    with patch("brain_triage.worker.chat", AsyncMock(side_effect=fake_chat)):
        out = await triage_group_batch(rows)
    assert out[1] is not None


@pytest.mark.asyncio
async def test_group_batch_passes_batch_schema_and_first_event_id():
    rows = [_row(5), _row(6)]
    captured = {}

    async def fake_chat(**kwargs):
        captured.update(kwargs)
        return json.dumps({"results": [_triage_item(5), _triage_item(6)]}), \
            {"provider": "x"}

    with patch("brain_triage.worker.chat", AsyncMock(side_effect=fake_chat)):
        await triage_group_batch(rows)

    assert captured["response_format"] == TRIAGE_BATCH_JSON_SCHEMA
    assert captured["event_id"] == 5   # первое событие chunk'а — для трейса
    assert captured["workflow"] == "triage"
