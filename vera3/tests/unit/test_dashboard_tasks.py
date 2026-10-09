"""`/tasks`: раскладка по вкладкам, экранирование, доступ только владельцу, 404 карточки."""
from __future__ import annotations

import base64
import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("TOKEN_SECRET", base64.urlsafe_b64encode(b"0" * 32).decode())
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("OWNER_TELEGRAM_ID", "169510539")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from dashboard.app import app  # noqa: E402
from dashboard.auth import COOKIE_NAME  # noqa: E402
from dashboard.auth_routes import _set_session_cookie  # noqa: E402
from dashboard.tasks_service import TaskItem, classify, split_tabs  # noqa: E402
from dashboard.tasks_view import task_detail, tasks_body  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.responses import Response  # noqa: E402
from vera_shared.room.attention import attention  # noqa: E402

NOW = datetime(2026, 10, 9, 12, 0)


def make_row(**kw):
    base = {"room": "r", "task_id": "T-1", "title": "Задача", "status": "in_progress",
                "lease_holder": "claude", "lease_until": NOW + timedelta(hours=1),
                "last_progress_at": NOW - timedelta(minutes=10), "last_progress_text": "шаг",
                "next_checkpoint_at": None, "waiting_until": None, "updated_at": NOW,
                "priority": 2, "owner": None, "holder_account": "acc", "project": None,
                "next_action": None, "refs": [], "created_by": "claude", "waiting_reason": None, "responsible": None,
                "plan_start": None, "plan_end": None}
    base.update(kw)
    return SimpleNamespace(**base)


def item(question=None, **kw):
    row = make_row(**kw)
    return TaskItem(row, attention(row, NOW, open_question_at=question))


def event(**kw):
    base = {"at": NOW, "kind": "progress", "agent": "claude", "text": "ок", "session": None, "data": None}
    base.update(kw)
    return SimpleNamespace(**base)


def cookie() -> str:
    resp = Response()
    _set_session_cookie(resp)
    return resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]


def test_classification_by_attention():
    assert classify(item().attention) == "work"
    assert classify(item(status="open", lease_holder=None, lease_until=None).attention) == "work"
    assert classify(item(question=NOW).attention) == "me"
    assert classify(item(lease_until=NOW - timedelta(hours=1)).attention) == "me"
    assert classify(item(last_progress_at=NOW - timedelta(hours=5)).attention) == "me"
    assert classify(item(status="done").attention) == "done"
    assert classify(item(status="cancelled").attention) == "done"


def test_sorted_by_priority_then_recent_activity():
    old = item(task_id="old", priority=1, last_progress_at=NOW - timedelta(minutes=50))
    new = item(task_id="new", priority=1, last_progress_at=NOW - timedelta(minutes=5))
    low = item(task_id="low", priority=0, last_progress_at=NOW - timedelta(minutes=55))
    ids = [i.row.task_id for i in split_tabs([old, new, low])["work"]]
    assert ids == ["low", "new", "old"]


def test_view_escapes_and_counts():
    evil = item(title="<script>x</script>", last_progress_text="<img src=x>")
    html = tasks_body("work", split_tabs([evil]), NOW)
    assert "<script>x" not in html and "<img src=x>" not in html
    assert "&lt;script&gt;" in html and "В работе 1" in html and "Нужен я 0" in html


def test_empty_state_is_plain():
    assert "Задач в работе пока нет" in tasks_body("work", split_tabs([]), NOW)


def test_detail_escapes_refs_events_and_blocks_js_urls():
    it = item(refs=[{"kind": "url", "ref": "javascript:alert(1)"},
                    {"kind": "jira", "ref": "<b>SIN-1</b>"}],
              next_action="<i>go</i>")
    html = task_detail(it, [event(text="<script>a</script>")], NOW)
    assert "<script>a" not in html and "<b>SIN-1" not in html and "<i>go" not in html
    assert 'href="javascript' not in html


def test_non_owner_is_redirected_or_401():
    client = TestClient(app, follow_redirects=False)
    r = client.get("/tasks")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/tasks/r/T-1").status_code == 401


def test_page_and_unknown_task_fragment():
    c = {COOKIE_NAME: cookie()}
    client = TestClient(app)
    with patch("dashboard.tasks_routes.load_tabs",
               AsyncMock(return_value=split_tabs([item(), item(task_id="d", status="done")]))),                patch("dashboard.tasks_routes.load_gantt_rows", AsyncMock(return_value=[])):
        r = client.get("/tasks?tab=done&span=bogus", cookies=c)
        r7 = client.get("/tasks?tab=done&span=7d", cookies=c)
    assert r.status_code == 200 and "Готово 1" in r.text and 'href="/tasks"' in r.text
    assert r7.status_code == 200 and 'href="/tasks?tab=work&amp;span=7d"' in r7.text
    with patch("dashboard.tasks_routes.load_detail", AsyncMock(return_value=None)):
        assert client.get("/tasks/r/nope", cookies=c).status_code == 404
