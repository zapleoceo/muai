"""`/approvals`: только владелец и только со страницы дашборда применяет правки агентов."""
from __future__ import annotations

import base64
import os
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("TOKEN_SECRET", base64.urlsafe_b64encode(b"0" * 32).decode())
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("OWNER_TELEGRAM_ID", "169510539")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest  # noqa: E402
from dashboard.app import app  # noqa: E402
from dashboard.approvals_view import approvals_body  # noqa: E402
from dashboard.auth import COOKIE_NAME  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from vera_shared.journal.proposals import ProposalNotPending  # noqa: E402

client = TestClient(app)
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}
DECIDE = "/api/approvals/decide"


def _cookie() -> dict[str, str]:
    from dashboard.auth_routes import _set_session_cookie
    from starlette.responses import Response
    resp = Response()
    _set_session_cookie(resp)
    return {COOKIE_NAME: resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]}


def test_decision_needs_the_owner():
    with patch("dashboard.approvals_routes.proposals.approve", AsyncMock()) as run:
        r = client.post(DECIDE, json={"kind": "proposal", "id": 1, "approve": True},
                        headers=SAME_ORIGIN)
    assert r.status_code == 401
    run.assert_not_awaited()


def test_cross_site_decision_is_refused_even_for_the_owner():
    with patch("dashboard.approvals_routes.proposals.approve", AsyncMock()) as run:
        r = client.post(DECIDE, json={"kind": "proposal", "id": 1, "approve": True},
                        cookies=_cookie(),
                        headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    run.assert_not_awaited()


def test_page_needs_the_owner():
    assert client.get("/approvals", follow_redirects=False).status_code in (303, 401, 403)


@pytest.mark.parametrize(("body", "target"), [
    ({"kind": "proposal", "id": 5, "approve": True}, "proposals.approve"),
    ({"kind": "proposal", "id": 5, "approve": False}, "proposals.reject"),
    ({"kind": "memory", "id": 5, "approve": True}, "agent_memory.confirm_agent_memory"),
    ({"kind": "memory", "id": 5, "approve": False}, "agent_memory.reject_agent_memory"),
])
def test_owner_decision_reaches_the_right_action(body, target):
    with patch(f"dashboard.approvals_routes.{target}",
               AsyncMock(return_value={"ok": True})) as run:
        r = client.post(DECIDE, json=body, cookies=_cookie(), headers=SAME_ORIGIN)
    assert r.status_code == 200
    assert run.await_args.args[0] == 5


def test_stale_decision_is_a_conflict():
    with patch("dashboard.approvals_routes.proposals.approve",
               AsyncMock(side_effect=ProposalNotPending(5))):
        r = client.post(DECIDE, json={"kind": "proposal", "id": 5, "approve": True},
                        cookies=_cookie(), headers=SAME_ORIGIN)
    assert r.status_code == 409 and "not pending" in r.json()["error"]


def test_page_escapes_agent_text():
    proposal = SimpleNamespace(
        id=3, client="claude", tool="hide_event", args={"event_id": 7}, target_id=7,
        created_at=datetime(2026, 11, 1, 10, 0),
        after={"preview": {"source": "gmail", "excerpt": "<script>x</script>"}})
    memory = SimpleNamespace(id=9, content_text="<img src=x onerror=1>",
                             occurred_at=datetime(2026, 11, 1, 10, 0))
    html = approvals_body([proposal], [memory])
    assert "<script>x</script>" not in html and "<img src=x" not in html
    assert 'data-kind="proposal" data-id="3"' in html
    assert 'data-kind="memory" data-id="9"' in html


def test_page_lists_pending_items_for_the_owner():
    with patch("dashboard.approvals_routes.proposals.pending", AsyncMock(return_value=[])), \
         patch("dashboard.approvals_routes.agent_memory.pending_agent_memories",
               AsyncMock(return_value=[])):
        html = client.get("/approvals", cookies=_cookie()).text
    assert "Подтверждения" in html and "Ждущих решения нет" in html
