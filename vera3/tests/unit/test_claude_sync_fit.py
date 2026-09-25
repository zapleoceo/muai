"""scripts/claude_chat_sync.py — длинная сессия ужимается под лимит тела nginx.

До 2026-09-25 56 из 63 ждавших сессий отбивались 413 каждый час, и контекст
соседних сессий Claude в мозг не попадал. Проверяем: влезает в бюджет, ход
сессии (начало и конец) сохранён, курсор считает все реплики.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "claude_chat_sync.py"


@pytest.fixture
def sync():
    spec = importlib.util.spec_from_file_location("claude_chat_sync", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _payload(n: int, chars: int) -> dict:
    return {"session_id": "s", "project_dir": "p", "started_at": "2026-09-25T00:00:00",
            "ended_at": "2026-09-25T01:00:00", "cwd": None, "git_branch": None,
            "turns": [{"role": "user" if i % 2 == 0 else "assistant",
                       "text": f"#{i} " + "ж" * chars} for i in range(n)]}


def _size(p: dict) -> int:
    return len(json.dumps(p, ensure_ascii=False).encode("utf-8"))


def test_small_session_goes_as_is(sync):
    p = _payload(10, 100)
    out = sync.fit_body(p)
    assert out["turns"] == p["turns"]
    assert out["turn_count"] == 10


def test_long_turns_are_shortened_first(sync):
    out = sync.fit_body(_payload(300, 4000))
    assert _size(out) <= sync.BODY_BUDGET_BYTES
    assert len(out["turns"]) == 300
    assert out["turn_count"] == 300


def test_huge_session_is_thinned_but_keeps_its_edges(sync):
    p = _payload(20_000, 4000)
    out = sync.fit_body(p)
    assert _size(out) <= sync.BODY_BUDGET_BYTES
    assert out["turn_count"] == 20_000
    assert out["turns"][0]["text"].startswith("#0 ")
    assert out["turns"][-1]["text"].startswith("#19999 ")
