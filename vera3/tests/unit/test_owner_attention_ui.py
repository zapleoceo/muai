"""Панель внимания и колонки таблицы задач; статичный HTML для скриншота."""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from dashboard.attention_view import attention_panel
from dashboard.tasks_service import TaskItem, split_tabs
from dashboard.tasks_table import HEAD, tasks_table
from dashboard.tasks_view import tasks_body
from vera_shared.owner_attention import store
from vera_shared.room.attention import attention

NOW = datetime(2026, 10, 10, 17, 30)  # 00:30 по Хошимину
OUT = Path(os.environ.get("OFFICE_UI_HTML", "d:/Temp/claude/office-ui.html"))


def _row(**kw):
    base = {"room": "r", "task_id": "T-1", "title": "Задача", "status": "in_progress",
            "lease_holder": "claude", "lease_until": NOW + timedelta(hours=1),
            "last_progress_at": NOW - timedelta(minutes=10), "last_progress_text": "шаг",
            "next_checkpoint_at": NOW + timedelta(hours=2), "waiting_until": None,
            "updated_at": NOW, "priority": 2, "owner": None, "holder_account": "acc",
            "project": "Vera", "next_action": "выкатить <b>панель</b>", "refs": [],
            "created_by": "claude", "waiting_reason": None, "responsible": "codex",
            "plan_start": None, "plan_end": None}
    base.update(kw)
    return SimpleNamespace(**base)


def _items() -> list[TaskItem]:
    rows = [_row(), _row(task_id="T-2", title="Заблокирована", status="blocked",
                         waiting_reason="нужен ключ", responsible="claude",
                         lease_holder=None, next_checkpoint_at=None, last_progress_at=None)]
    return [TaskItem(r, attention(r, NOW)) for r in rows]


def test_panel_states_flag_and_queue():
    deferred = [store.Deferred("k", "alert", "T-2 <ждёт>", NOW)]
    quiet = attention_panel(store.Attention(store.QUIET, quiet_until=NOW + timedelta(hours=8)),
                            deferred, enabled=True)
    assert "quiet до 08:30" in quiet and "OWNER_QUIET_ENABLED: вкл" in quiet
    assert "T-2 &lt;ждёт&gt;" in quiet and "Отложено (1)" in quiet
    wait = attention_panel(store.Attention(store.WAIT10, wait_deadline=NOW + timedelta(minutes=10)),
                           [], enabled=False)
    assert "wait10 (до 00:40)" in wait and "выкл" in wait and "пуста" in wait
    assert "active" in attention_panel(store.Attention(), [], enabled=False)


def test_table_columns_and_escaping():
    html = tasks_table(_items(), NOW)
    for head in HEAD:
        assert f"<th>{head}</th>" in html
    assert "выкатить &lt;b&gt;панель&lt;/b&gt;" in html
    assert "нужен ключ" in html
    assert "2026-10-11 02:30" in html  # контрольная точка в местном времени


def test_render_static_page():
    panel = attention_panel(store.Attention(store.QUIET, quiet_until=NOW + timedelta(hours=8)),
                            [store.Deferred("k", "alert", "T-2 ждёт ответа", NOW)], enabled=True)
    body = tasks_body("work", split_tabs(_items()), NOW, attention=panel)
    assert "Внимание владельца" in body and "tk-table" in body
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(f'<!doctype html><meta charset="utf-8"><body>{body}</body>', encoding="utf-8")


@pytest.mark.asyncio
async def test_panel_flag_off_does_not_touch_tables(monkeypatch):
    from unittest.mock import AsyncMock, patch

    from dashboard.attention_view import OFF_TEXT, attention_html
    monkeypatch.setenv("OWNER_QUIET_ENABLED", "0")
    with patch("vera_shared.owner_attention.store.load_state", AsyncMock(side_effect=AssertionError)):
        assert OFF_TEXT in await attention_html()


@pytest.mark.asyncio
async def test_panel_flag_on_missing_table_logs_and_degrades(monkeypatch, caplog):
    from unittest.mock import AsyncMock, patch

    from dashboard.attention_view import BROKEN_TEXT, attention_html
    from sqlalchemy.exc import OperationalError
    monkeypatch.setenv("OWNER_QUIET_ENABLED", "1")
    err = OperationalError("select", {}, Exception("no such table: owner_attention"))
    with patch("vera_shared.owner_attention.store.load_state", AsyncMock(side_effect=err)):
        html = await attention_html()
    assert BROKEN_TEXT in html
    assert any(r.levelname == "WARNING" for r in caplog.records)
