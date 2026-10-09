"""Поле responsible: открытие, обновление, лимиты, показ на /tasks."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from dashboard.tasks_service import TaskItem
from dashboard.tasks_view import task_detail, task_row
from vera_mcp import room_tools as r
from vera_shared.room.attention import attention
from vera_shared.room.task_fields import clean_responsible

NOW = datetime(2026, 10, 9, 12, 0)


def ctx(client: str):
    return SimpleNamespace(request_context=SimpleNamespace(
        request=SimpleNamespace(scope={"mcp_client": client})))


CLAUDE = ctx("claude")


def test_clean_responsible_strips_and_limits():
    assert clean_responsible("  Дима ") == "Дима"
    assert clean_responsible("   ") is None
    assert clean_responsible(None) is None
    with pytest.raises(ValueError):
        clean_responsible("x" * 129)


@pytest.mark.asyncio
async def test_open_and_update_set_responsible(sqlite_db):
    opened = await r.room_task_open("t1", CLAUDE, responsible=" Дима ")
    assert opened["task"]["responsible"] == "Дима"
    claimed = await r.room_task_claim("t1", CLAUDE)
    token = claimed["task"]["fencing_token"]
    same = await r.room_task_update("t1", token, CLAUDE, responsible="")
    assert same["task"]["responsible"] == "Дима"
    changed = await r.room_task_update("t1", token, CLAUDE, responsible="codex")
    assert changed["task"]["responsible"] == "codex"
    listed = (await r.room_tasks())["tasks"]
    assert listed[0]["responsible"] == "codex"
    assert (await r.room_task_open("t2", CLAUDE))["task"]["responsible"] is None


def _row(responsible):
    return SimpleNamespace(
        room="r", task_id="T-1", title="З", status="in_progress", lease_holder="claude",
        lease_until=datetime(2026, 10, 9, 13), last_progress_at=None, last_progress_text=None,
        next_checkpoint_at=None, waiting_until=None, updated_at=NOW, priority=2, owner=None,
        holder_account=None, project=None, next_action=None, refs=[], created_by="c",
        waiting_reason=None, plan_start=None, plan_end=None, responsible=responsible)


def test_dashboard_shows_responsible_escaped():
    row = _row("<b>Дима</b>")
    it = TaskItem(row, attention(row, NOW))
    assert "отв.: &lt;b&gt;Дима" in task_row(it, NOW)
    assert "Ответственный" in task_detail(it, [], NOW)
    none = _row(None)
    assert "отв.:" not in task_row(TaskItem(none, attention(none, NOW)), NOW)
