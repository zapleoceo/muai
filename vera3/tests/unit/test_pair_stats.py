"""pair_stats: чтение кэша, цикл пересборки, освобождение устоявшихся пар в чистке, подписи.

Пересборка — Postgres-only SQL, её проверяет integration/test_pair_stats_pg.py.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from brain_triage import pair_stats_loop as loop
from dashboard.graph_labels import predicate_label, role_label
from dashboard.graph_page import graph_body
from sqlalchemy.exc import OperationalError
from vera_shared.db.models_graph import PairStatsRow
from vera_shared.graph import pair_stats
from vera_shared.graph.rel_cleanup_verify import (
    RULE_ESTABLISHED,
    load_cache,
    verify_plan,
)
from vera_shared.graph.rel_verify import EdgeQuery

BROKER = "vera_shared.graph.rel_verify.chat_async"


@pytest.mark.asyncio
async def test_partner_stats_reads_both_sides_of_the_ordered_pair(sqlite_db):
    async with sqlite_db() as s:
        s.add(PairStatsRow(entity_a=1, entity_b=2, dm_msgs=9, dm_days=3, active_days=3,
                           first_at=datetime(2026, 1, 1), last_at=datetime(2026, 2, 1)))
        s.add(PairStatsRow(entity_a=2, entity_b=3, co_days=5, active_days=5))
    got = await pair_stats.partner_stats(2)
    assert set(got) == {1, 3}
    assert got[1].dm_msgs == 9 and got[1].first_at == datetime(2026, 1, 1)
    assert got[3].co_days == 5 and got[3].first_at is None
    assert got[1].as_dict()["last_at"] == "2026-02-01T00:00:00"


@pytest.mark.asyncio
async def test_stats_within_needs_both_ends_in_the_set(sqlite_db):
    async with sqlite_db() as s:
        s.add(PairStatsRow(entity_a=1, entity_b=2, active_days=1))
        s.add(PairStatsRow(entity_a=2, entity_b=3, active_days=1))
    assert set(await pair_stats.stats_within([1, 2])) == {(1, 2)}
    assert await pair_stats.stats_within([1]) == {}
    assert pair_stats.ordered(5, 2) == (2, 5)


def test_chat_size_cap_and_work_projects_are_documented_constants():
    assert pair_stats.MAX_CHAT_AUTHORS == 60
    assert set(pair_stats.WORK_PROJECTS) == {"itstep", "veranda"}


@pytest.mark.asyncio
async def test_loop_survives_a_failed_refresh_and_keeps_running():
    calls = []

    async def refresh():
        calls.append(1)
        if len(calls) == 1:
            raise OperationalError("select", {}, Exception("no table"))
        if len(calls) == 2:
            return None
        return 12

    async def stop_after_three(_seconds):
        if len(calls) >= 3:
            raise asyncio.CancelledError

    with patch.object(loop, "refresh_pair_stats", refresh), \
         patch.object(loop.asyncio, "sleep", stop_after_three), \
         pytest.raises(asyncio.CancelledError):
        await loop.pair_stats_loop()
    assert len(calls) == 3


def _cand(rel_id: int, subject: int, obj: int) -> dict:
    return {"id": rel_id, "predicate": "boss_of", "subject_entity_id": subject,
            "object_entity_id": obj, "is_current": True, "subject_name": "Лиза",
            "object_name": "Игорь", "derived_from_event_id": rel_id, "fact": "f"}


@pytest.mark.asyncio
async def test_established_pairs_are_exempt_from_the_model_check(sqlite_db, tmp_path):
    cands = [_cand(1, 5, 9), _cand(2, 3, 4)]
    cache = tmp_path / "plan.verdicts.jsonl"
    broker = AsyncMock(return_value=(json.dumps({"verdict": "no", "quote": ""}),
                                     {"cost_usd": 0.0}))
    with patch(BROKER, broker):
        actions, stats = await verify_plan(cands, cache, established={(5, 9)})
    skipped = [a for a in actions if a["rule"] == RULE_ESTABLISHED]
    assert [a["rel_id"] for a in skipped] == [1] and skipped[0]["action"] == "skip"
    assert stats["established"] == 1 and stats["candidates"] == 2
    assert broker.await_count == 0 and stats["unverified"] == 1   # без текста события — не судим
    assert EdgeQuery(1, "Лиза", "boss_of", "Игорь").key not in load_cache(cache)


@pytest.mark.asyncio
async def test_exempt_pairs_do_not_eat_the_trial_limit(sqlite_db, tmp_path):
    cands = [_cand(1, 5, 9), _cand(2, 3, 4), _cand(3, 6, 7)]
    _, stats = await verify_plan(cands, tmp_path / "p.jsonl", limit=1, established={(5, 9)})
    assert stats["checked"] == 1 and stats["established"] == 1


def test_role_labels_name_what_the_other_is_to_the_viewer():
    assert role_label("boss_of", "in") == "начальник"
    assert role_label("boss_of", "out") == "подчинённый"
    assert role_label("parent_of", "in") == "родитель"
    assert role_label("client_of", "in") == "клиент" and role_label("vendor_of", "in") == "поставщик"
    assert role_label("coworker_of", "both") == predicate_label("coworker_of") == "работает с"


def test_page_script_carries_connection_rows_and_edge_weights():
    body = graph_body(["boss_of"], None)
    assert "function connRow(c)" in body and "возможно тот же человек" in body
    assert "e.data('weight')" in body and "также:" in body
    assert "__SCRIPT__" not in body and "__PRED_LABELS__" not in body
