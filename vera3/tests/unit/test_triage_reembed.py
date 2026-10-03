"""reembed: доэмбеддинг событий без вектора — порция, курсор, цикл брокера."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from brain_triage import reembed

HEAD = "Author: a\nFrom: a\nChat: c (user)\nDate: d\nDirection: received\n---\n"


def _rows(*pairs: tuple[int, str]) -> list[tuple[int, str]]:
    return [(i, HEAD + body) for i, body in pairs]


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(reembed, "REEMBED_BATCH", 2)
    cooldown = AsyncMock(return_value=0.0)
    fetch = AsyncMock()
    embed = AsyncMock(side_effect=lambda texts: [[0.1] for _ in texts])
    write = AsyncMock(side_effect=lambda pairs: len(pairs))
    chunks = AsyncMock()
    with (patch.object(reembed, "llm_cooldown_remaining_s", cooldown),
          patch.object(reembed, "_fetch_candidates", fetch),
          patch.object(reembed, "_embed_batch", embed),
          patch.object(reembed, "write_embeddings", write),
          patch.object(reembed, "embed_event_chunks", chunks)):
        yield {"cooldown": cooldown, "fetch": fetch, "embed": embed,
               "write": write, "chunks": chunks}


@pytest.mark.asyncio
async def test_skips_contentless_and_embeds_the_rest(env):
    env["fetch"].return_value = _rows((9, "[photo]"), (8, "Ок"),
                                      (7, "настоящее сообщение про сделку"))
    cursor, written = await reembed.reembed_once()
    assert written == 1
    assert [p[0] for p in env["write"].await_args.args[0]] == [7]
    assert cursor is None   # выборка короче лимита — проход закончен


@pytest.mark.asyncio
async def test_cursor_walks_down_past_placeholders(env):
    """Заглушки не выедают порцию: курсор уходит ниже, а не топчется на месте."""
    full = _rows(*[(100 - i, "[voice]") for i in range(8)])
    env["fetch"].return_value = full
    cursor, written = await reembed.reembed_once()
    assert written == 0 and cursor == 93
    env["fetch"].return_value = _rows((90, "содержательное сообщение номер раз"),
                                      (89, "содержательное сообщение номер два"),
                                      (88, "содержательное сообщение номер три"))
    cursor, written = await reembed.reembed_once(cursor)
    assert env["fetch"].await_args.args[0] == 93
    assert written == 2 and cursor == 89   # порция выбрана, курсор после неё


@pytest.mark.asyncio
async def test_circuit_open_does_nothing(env):
    env["cooldown"].return_value = 120.0
    assert await reembed.reembed_once(55) == (55, 0)
    env["fetch"].assert_not_awaited()


@pytest.mark.asyncio
async def test_embed_failure_keeps_cursor(env):
    env["fetch"].return_value = _rows((7, "настоящее сообщение про сделку"))
    env["embed"].side_effect = lambda texts: [None for _ in texts]
    assert await reembed.reembed_once(50) == (50, 0)
    env["write"].assert_not_awaited()
