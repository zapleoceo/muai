"""extract_and_store: проверка до записи и счётчики прогона на SQLite.

С 2026-09-04 rel-extract не создавал ни одной связи, а все пути отказа
логировались на DEBUG — поломку было не видно. Теперь итог каждого прогона
— INFO со счётчиками, отказ LLM — WARNING.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text
from vera_shared.graph import repo
from vera_shared.graph.rel_extract import extract_and_store
from vera_shared.llm.client import LLMCallFailed

BODY = "Ольга работает в IT STEP, а Link работает в OpenRouter"


async def _entity(type_: str, name: str, ident: str) -> int:
    return await repo.upsert_entity(type=type_, name=name, source="telegram",
                                    identifier=ident)


def _reply(*rels: tuple[str, str, str]) -> str:
    return json.dumps({"relationships": [
        {"subject": s, "predicate": p, "object": o, "fact": "f", "confidence": 0.9}
        for s, p, o in rels]})


async def _run(reply, event_id: int = 1):
    mock = AsyncMock(side_effect=reply) if isinstance(reply, Exception) \
        else AsyncMock(return_value=(reply, {}))
    with patch("vera_shared.graph.rel_extract.chat_async", mock):
        return await extract_and_store(event_id, BODY)


async def _rels(get_session) -> list[tuple[int, int, str]]:
    async with get_session() as s:
        return [tuple(r) for r in (await s.execute(text(
            "SELECT subject_entity_id, object_entity_id, predicate FROM relationships"
        ))).all()]


@pytest.mark.asyncio
async def test_valid_written_junk_rejected_and_counted(sqlite_db, caplog):
    olga = await _entity("person", "Ольга", "user:1")
    itstep = await _entity("organization", "IT STEP", "org:1")
    await _entity("person", "Link", "user:2")
    await _entity("person", "OpenRouter, Inc", "user:3")

    with caplog.at_level("INFO"):
        out = await _run(_reply(
            ("Ольга", "works_at", "IT STEP"),
            ("Link", "works_at", "OpenRouter, Inc"),     # объект — персона
            ("Никто", "friend_of", "Ольга"),             # в графе нет
        ))

    assert (out.returned, out.inserted, out.unresolved) == (3, 1, 1)
    assert dict(out.rejected) == {"type_mismatch": 1}
    assert await _rels(sqlite_db) == [(olga, itstep, "works_at")]
    assert "вернула=3 не_найдено=1 отсеяно=1" in caplog.text
    assert "записано=1" in caplog.text


@pytest.mark.asyncio
async def test_empty_answer_is_visible_at_info(sqlite_db, caplog):
    with caplog.at_level("INFO"):
        out = await _run(_reply())
    assert out.returned == 0 and out.inserted == 0
    assert any(r.levelname == "INFO" and "записано=0" in r.getMessage()
               for r in caplog.records)


@pytest.mark.asyncio
async def test_llm_failure_is_warning(sqlite_db, caplog):
    out = await _run(LLMCallFailed("пул пуст"))
    assert out.llm_failed is True
    assert any(r.levelname == "WARNING" and "LLM не ответила" in r.getMessage()
               for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["не json", '{"relationships": "x"}', "[]"])
async def test_broken_answer_is_warning_without_content(sqlite_db, caplog, raw):
    out = await _run(raw)
    assert out.llm_failed is True
    warn = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert warn and "не по схеме" in warn[0]
    assert raw not in warn[0]


@pytest.mark.asyncio
async def test_malformed_items_counted(sqlite_db):
    out = await _run(json.dumps({"relationships": [
        {"subject": "", "predicate": "works_at", "object": "X"},
        {"subject": "A", "predicate": "enemy_of", "object": "B"}]}))
    assert out.rejected["malformed"] == 2


@pytest.mark.asyncio
async def test_blank_body_skips_llm(sqlite_db):
    mock = AsyncMock()
    with patch("vera_shared.graph.rel_extract.chat_async", mock):
        out = await extract_and_store(1, "   ")
    mock.assert_not_awaited()
    assert out.returned == 0
