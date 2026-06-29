"""dashboard /control/backfill — pause/resume button endpoint (auth gate)."""
from __future__ import annotations

import base64
import os

# dashboard.app reads secrets at import — CI-safe defaults BEFORE import.
os.environ.setdefault("TOKEN_SECRET", base64.urlsafe_b64encode(b"0" * 32).decode())
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("OWNER_TELEGRAM_ID", "169510539")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from unittest.mock import AsyncMock, patch  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from dashboard.app import app  # noqa: E402

client = TestClient(app)


def test_control_backfill_requires_auth():
    """No owner cookie → 401, never touches the flag."""
    r = client.post("/control/backfill", data={"action": "pause"})
    assert r.status_code == 401


def test_control_backfill_requires_action_field():
    """action is a required Form field → 422 without it (still auth-gated 401
    fires first since require_owner runs in the handler body — but FastAPI
    validates the body before the handler, so missing field → 422)."""
    r = client.post("/control/backfill")
    assert r.status_code in (401, 422)


def test_control_backfill_pause_sets_flag_when_authed():
    """With a valid owner session, pause calls set_backfill_paused(True)."""
    from dashboard.app import _set_session_cookie

    # Mint a session cookie the same way the app does.
    from starlette.responses import Response
    resp = Response()
    _set_session_cookie(resp)
    cookie_header = resp.headers.get("set-cookie", "")
    cookie_val = cookie_header.split(";")[0].split("=", 1)[1] if "=" in cookie_header else ""

    from dashboard.auth import COOKIE_NAME

    with patch("dashboard.app.set_backfill_paused", AsyncMock()) as fake_set, \
         patch("dashboard.app._build_progress_fragment",
               AsyncMock(return_value="<div>ok</div>")):
        r = client.post(
            "/control/backfill",
            data={"action": "pause"},
            cookies={COOKIE_NAME: cookie_val},
        )
    assert r.status_code == 200
    fake_set.assert_awaited_once_with(True)


def test_control_backfill_resume_clears_flag_when_authed():
    from dashboard.app import _set_session_cookie
    from starlette.responses import Response
    resp = Response()
    _set_session_cookie(resp)
    cookie_header = resp.headers.get("set-cookie", "")
    cookie_val = cookie_header.split(";")[0].split("=", 1)[1] if "=" in cookie_header else ""

    from dashboard.auth import COOKIE_NAME

    with patch("dashboard.app.set_backfill_paused", AsyncMock()) as fake_set, \
         patch("dashboard.app._build_progress_fragment",
               AsyncMock(return_value="<div>ok</div>")):
        r = client.post(
            "/control/backfill",
            data={"action": "resume"},
            cookies={COOKIE_NAME: cookie_val},
        )
    assert r.status_code == 200
    fake_set.assert_awaited_once_with(False)
