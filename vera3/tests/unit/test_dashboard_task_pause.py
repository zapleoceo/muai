"""POST /tasks/{room}/{task_id}/pause|resume: ворота владельца и same-origin, коды ошибок."""
from __future__ import annotations

import base64
import os
from unittest.mock import AsyncMock, patch

os.environ.setdefault("TOKEN_SECRET", base64.urlsafe_b64encode(b"0" * 32).decode())
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("OWNER_TELEGRAM_ID", "169510539")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest  # noqa: E402
from dashboard.app import app  # noqa: E402
from dashboard.auth import COOKIE_NAME  # noqa: E402
from dashboard.auth_routes import _set_session_cookie  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.responses import Response  # noqa: E402
from vera_shared.room.tasks import TaskBusy, TaskNotFound  # noqa: E402

SAME = {"origin": "http://testserver"}
ACTIONS = [("pause", True), ("resume", False)]


def cookie() -> dict[str, str]:
    resp = Response()
    _set_session_cookie(resp)
    return {COOKIE_NAME: resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]}


def post(action, headers=None, cookies=None, data=None):
    client = TestClient(app, follow_redirects=False)
    form = {"reason": "пауза"} if data is None else data
    return client.post(f"/tasks/main/T-1/{action}", data=form,
                       headers=headers or {}, cookies=cookies or {})


@pytest.mark.parametrize("action", ["pause", "resume"])
def test_non_owner_is_refused_before_anything_is_written(action):
    with patch("dashboard.tasks_routes.owner_pause", AsyncMock()) as op:
        assert post(action, headers=SAME).status_code == 401
        op.assert_not_called()


@pytest.mark.parametrize("action", ["pause", "resume"])
def test_cross_origin_owner_post_is_refused(action):
    with patch("dashboard.tasks_routes.owner_pause", AsyncMock()) as op:
        evil = post(action, headers={"origin": "https://evil.example"}, cookies=cookie())
        assert evil.status_code == 403
        assert post(action, cookies=cookie()).status_code == 403  # ни Origin, ни Sec-Fetch-Site
        op.assert_not_called()


@pytest.mark.parametrize(("action", "paused"), ACTIONS)
def test_owner_same_origin_calls_service_with_reason(action, paused):
    with patch("dashboard.tasks_routes.owner_pause", AsyncMock()) as op, \
            patch("dashboard.tasks_routes.load_detail", AsyncMock(return_value=None)):
        r = post(action, headers=SAME, cookies=cookie())
        op.assert_awaited_once_with("main", "T-1", "пауза", paused=paused)
        assert r.status_code == 404  # карточка не найдена -> 404, действие уже записано


@pytest.mark.parametrize("action", ["pause", "resume"])
def test_service_errors_map_to_http_codes(action):
    for exc, code in ((TaskNotFound("main", "T-1"), 404), (TaskBusy("done"), 409),
                      (ValueError("reason must not be empty"), 422)):
        with patch("dashboard.tasks_routes.owner_pause", AsyncMock(side_effect=exc)):
            assert post(action, headers=SAME, cookies=cookie()).status_code == code


@pytest.mark.parametrize("action", ["pause", "resume"])
def test_missing_reason_is_422(action):
    with patch("dashboard.tasks_routes.owner_pause", AsyncMock()) as op:
        assert post(action, headers=SAME, cookies=cookie(), data={}).status_code == 422
        op.assert_not_called()
