"""dashboard instagram_login /api/instagram/verify — flow survives a
failed attempt.

Regression: verify() used to unconditionally pop() the flow before
trying the code, so ANY failure (wrong code, or Instagram's own
feedback_required rate-limit — unrelated to the code itself) made the
immediate retry form always show "Флоу истёк" instead of the real
error, even though the form still pointed at that same flow_id."""
from __future__ import annotations

import base64
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("TOKEN_SECRET", base64.urlsafe_b64encode(b"0" * 32).decode())
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("OWNER_TELEGRAM_ID", "169510539")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from dashboard.app import app  # noqa: E402
from dashboard.auth import COOKIE_NAME  # noqa: E402
from dashboard.instagram_login import _flows  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app)


def _owner_cookie() -> str:
    from dashboard.auth_routes import _set_session_cookie
    from starlette.responses import Response
    resp = Response()
    _set_session_cookie(resp)
    ch = resp.headers.get("set-cookie", "")
    return ch.split(";")[0].split("=", 1)[1] if "=" in ch else ""


def test_failed_verify_keeps_flow_alive_for_retry():
    # ts must be a fresh monotonic reading — _prune_flows() drops anything
    # older than _FLOW_TTL_S, and ts=0 is always "expired" (monotonic() is a
    # large uptime-based number), which would wrongly 400 before the handler
    # even reaches the flow.
    _flows["testflow"] = {"client": MagicMock(), "kind": "2fa",
                          "username": "u", "password": "p",
                          "ts": time.monotonic()}

    with patch("dashboard.instagram_login._run_in_thread",
               AsyncMock(side_effect=Exception("feedback_required"))):
        r = client.post("/api/instagram/verify",
                        data={"flow_id": "testflow", "code": "123456"},
                        cookies={COOKIE_NAME: _owner_cookie()})

    assert r.status_code == 200
    assert "feedback_required" in r.text
    assert "testflow" in _flows            # flow must still be alive
    assert "testflow" in r.text            # retry form still points at it
    _flows.pop("testflow", None)


def test_successful_verify_removes_flow():
    fake_client = MagicMock()
    _flows["testflow2"] = {"client": fake_client, "kind": "2fa",
                           "username": "u", "password": "p",
                           "ts": time.monotonic()}

    with patch("dashboard.instagram_login._run_in_thread", AsyncMock()), \
         patch("dashboard.instagram_login._save_session", AsyncMock()):
        r = client.post("/api/instagram/verify",
                        data={"flow_id": "testflow2", "code": "123456"},
                        cookies={COOKIE_NAME: _owner_cookie()})

    assert r.status_code == 200
    assert "testflow2" not in _flows


def test_verify_unknown_flow_returns_400():
    r = client.post("/api/instagram/verify",
                    data={"flow_id": "does-not-exist", "code": "1"},
                    cookies={COOKIE_NAME: _owner_cookie()})
    assert r.status_code == 400
