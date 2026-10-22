"""Owner gates and data selection for the Office kanban endpoints."""
from __future__ import annotations

import base64
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import Request
from fastapi.responses import HTMLResponse

os.environ.setdefault("TOKEN_SECRET", base64.urlsafe_b64encode(b"0" * 32).decode())
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("OWNER_TELEGRAM_ID", "169510539")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from dashboard.office_hourly_routes import office_hourly, office_hourly_detail
from dashboard.office_hourly_service import HourlyReport

NOW = datetime(2026, 10, 22, 8, 0, tzinfo=UTC)


def _request() -> Request:
    return Request({"type": "http", "method": "GET", "path": "/office/hourly",
                    "headers": []})


def _finish(coroutine):
    """All awaits are mocked with immediately completed coroutines."""
    try:
        coroutine.send(None)
    except StopIteration as complete:
        return complete.value
    raise AssertionError("unexpected pending I/O in route unit test")


def _empty_report() -> HourlyReport:
    now = NOW
    return HourlyReport(now, now - timedelta(hours=1), [], [], {
        "Новая / в очереди": 0, "В работе": 0, "Блокер": 0, "Ревью": 0})


def test_kanban_route_requires_owner_and_passes_closed_filter() -> None:
    denied = HTMLResponse("denied", status_code=401)
    with patch("dashboard.office_hourly_routes.owner_or_redirect", return_value=denied), patch(
            "dashboard.office_hourly_routes.load_report", AsyncMock()) as load:
        assert _finish(office_hourly(_request())) is denied
        load.assert_not_awaited()
    with patch("dashboard.office_hourly_routes.owner_or_redirect", return_value=None), patch(
            "dashboard.office_hourly_routes.load_report",
            AsyncMock(return_value=_empty_report())) as load, patch(
                "dashboard.office_hourly_routes._render",
                side_effect=lambda _active, body, wide: body):
        response = _finish(office_hourly(_request(), show_closed=True))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert 'aria-label="На проверке"' in response.body.decode()
    load.assert_awaited_once_with(include_closed=True)


def test_modal_route_requires_owner_and_shows_recent_history_without_write_form() -> None:
    denied = HTMLResponse("denied", status_code=401)
    with patch("dashboard.office_hourly_routes.owner_or_blank_401", return_value=denied), patch(
            "dashboard.office_hourly_routes.load_detail", AsyncMock()) as load:
        assert _finish(office_hourly_detail(_request(), "main", "T-1")) is denied
        load.assert_not_awaited()
    row = SimpleNamespace(room="main", task_id="T-1", note="Описание",
                          status="open", responsible="dot", lease_holder=None,
                          last_progress_at=NOW, last_progress_text="Проверено",
                          next_action="Следующий шаг", waiting_reason=None,
                          depends_on=[], refs=[])
    event = SimpleNamespace(at=NOW, kind="progress", agent="dot", text="История")
    with patch("dashboard.office_hourly_routes.owner_or_blank_401", return_value=None), patch(
            "dashboard.office_hourly_routes.load_detail",
            AsyncMock(return_value=(SimpleNamespace(row=row), [], []))), patch(
                "dashboard.office_hourly_routes.load_recent_task_events",
                AsyncMock(return_value=[event])) as recent:
        response = _finish(office_hourly_detail(_request(), "main", "T-1"))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert "Описание" in response.body.decode() and "История" in response.body.decode()
    assert "Проверено" in response.body.decode() and "<form" not in response.body.decode()
    recent.assert_awaited_once_with("main", "T-1")
