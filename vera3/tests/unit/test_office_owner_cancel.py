"""Массовое применение отмен владельца из записи Room dot: отменяет каждую, ошибки считает."""
from __future__ import annotations

import base64
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("TOKEN_SECRET", base64.urlsafe_b64encode(b"0" * 32).decode())
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("OWNER_TELEGRAM_ID", "169510539")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest  # noqa: E402
from dashboard.office_hourly_service import HourlyReport  # noqa: E402
from dashboard.office_owner_cancel import apply_pending_cancellations  # noqa: E402
from vera_shared.room.tasks import TaskBusy  # noqa: E402

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 11, 11, 8, 0, tzinfo=UTC)


def report(pending) -> HourlyReport:
    rep = HourlyReport(NOW, NOW - timedelta(hours=1), [], [], {})
    rep.pending_cancellations = pending
    return rep


def row(task_id: str) -> SimpleNamespace:
    return SimpleNamespace(room="main", task_id=task_id)


async def test_each_pending_decision_is_cancelled_with_its_source():
    cancel = AsyncMock(side_effect=[{"changed": True}, {"changed": False},
                                    TaskBusy("attested")])
    pending = [(row("A"), "room:12"), (row("B"), "room:12"), (row("C"), "room:13")]
    with patch("dashboard.office_owner_cancel.load_report",
               AsyncMock(return_value=report(pending))), patch(
                   "dashboard.office_owner_cancel.cancel_by_owner", cancel):
        assert await apply_pending_cancellations() == {"cancelled": 1, "failed": 1}
    first = cancel.await_args_list[0].kwargs
    assert first == {"room": "main", "task_id": "A",
                     "reason": "Отмена владельца по решению room:12"}


async def test_no_pending_decisions_means_no_calls():
    cancel = AsyncMock()
    with patch("dashboard.office_owner_cancel.load_report",
               AsyncMock(return_value=report(None))), patch(
                   "dashboard.office_owner_cancel.cancel_by_owner", cancel):
        assert await apply_pending_cancellations() == {"cancelled": 0, "failed": 0}
    cancel.assert_not_called()
