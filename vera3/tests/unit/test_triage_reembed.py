"""reembed: голова (свежие id) + хвост (окно под курсором), оба по диапазону."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from brain_triage import reembed

HEAD = "Author: a\nFrom: a\nChat: c (user)\nDate: d\nDirection: received\n---\n"
GOOD = "настоящее сообщение про сделку"


def _tg(i: int, body: str) -> tuple[int, str, str]:
    return (i, "telegram", HEAD + body)


class FakeTable:
    """Кандидаты по id; _fetch_candidates фильтрует по [lo, hi), как SQL."""

    def __init__(self, rows: list[tuple[int, str, str]]):
        self.rows = rows
        self.ranges: list[tuple[int, int]] = []

    async def fetch(self, lo: int, hi: int, limit: int):
        self.ranges.append((lo, hi))
        got = sorted((r for r in self.rows if lo <= r[0] < hi), reverse=True)
        return got[:limit]


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(reembed, "REEMBED_BATCH", 2)
    monkeypatch.setattr(reembed, "HEAD_SPAN", 100)
    monkeypatch.setattr(reembed, "TAIL_SPAN", 1000)
    table = FakeTable([])
    mocks = {
        "table": table,
        "cooldown": AsyncMock(return_value=0.0),
        "max": AsyncMock(return_value=5000),
        "embed": AsyncMock(side_effect=lambda texts: [[0.1] for _ in texts]),
        "write": AsyncMock(side_effect=lambda pairs: len(pairs)),
        "chunks": AsyncMock(),
    }
    with (patch.object(reembed, "llm_cooldown_remaining_s", mocks["cooldown"]),
          patch.object(reembed, "_max_event_id", mocks["max"]),
          patch.object(reembed, "_fetch_candidates", table.fetch),
          patch.object(reembed, "_embed_batch", mocks["embed"]),
          patch.object(reembed, "write_embeddings", mocks["write"]),
          patch.object(reembed, "embed_event_chunks", mocks["chunks"])):
        yield mocks


def _written_ids(env) -> list[int]:
    return [p[0] for call in env["write"].await_args_list for p in call.args[0]]


@pytest.mark.asyncio
async def test_fresh_gap_closed_in_one_cycle_even_with_cursor_deep_down(env):
    """Дыра в самых свежих id не ждёт, пока курсор хвоста дойдёт до верха."""
    env["table"].rows = [_tg(4990, GOOD), _tg(1200, GOOD)]
    cursor, written = await reembed.reembed_once(cursor=1500)
    assert 4990 in _written_ids(env)
    assert written == 2 and 1200 in _written_ids(env)


@pytest.mark.asyncio
async def test_skips_contentless_chat_rows(env):
    env["table"].rows = [_tg(4999, "[photo]"), _tg(4998, "Ок"), _tg(4997, GOOD)]
    _, written = await reembed.reembed_once()
    assert written == 1 and _written_ids(env) == [4997]


@pytest.mark.asyncio
async def test_placeholders_in_head_do_not_starve_real_gap(env):
    env["table"].rows = [_tg(i, "[voice]") for i in range(4950, 5000)] + [_tg(4905, GOOD)]
    await reembed.reembed_once()
    assert _written_ids(env) == [4905]


@pytest.mark.asyncio
async def test_claude_memory_is_embedded_not_skipped(env):
    env["table"].rows = [(4990, "claude", "Купил хлеб")]
    await reembed.reembed_once()
    assert _written_ids(env) == [4990]


@pytest.mark.asyncio
async def test_every_query_is_range_bounded(env):
    await reembed.reembed_once(cursor=3000)
    for lo, hi in env["table"].ranges:
        assert hi - lo <= max(reembed.HEAD_SPAN, reembed.TAIL_SPAN) + 1


@pytest.mark.asyncio
async def test_tail_cursor_walks_down_and_resets_at_bottom(env):
    cursor = None
    seen: list[int | None] = []
    for _ in range(10):
        cursor, _ = await reembed.reembed_once(cursor)
        seen.append(cursor)
        if cursor is None:
            break
    assert seen[0] == 4901 - 1000
    assert seen[-1] is None
    assert len(seen) <= 6


@pytest.mark.asyncio
async def test_circuit_open_does_nothing(env):
    env["cooldown"].return_value = 120.0
    assert await reembed.reembed_once(55) == (55, 0)
    env["max"].assert_not_awaited()


@pytest.mark.asyncio
async def test_embed_failure_keeps_cursor(env):
    env["table"].rows = [_tg(4990, GOOD)]
    env["embed"].side_effect = lambda texts: [None for _ in texts]
    assert await reembed.reembed_once(50) == (50, 0)
    env["write"].assert_not_awaited()
