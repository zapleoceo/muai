"""Owner-only mobile projection, no writes and no independent task selection."""
from __future__ import annotations

import base64
import json
import os
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from fastapi import Request
from fastapi.responses import JSONResponse

os.environ.setdefault("TOKEN_SECRET", base64.urlsafe_b64encode(b"0" * 32).decode())
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("OWNER_TELEGRAM_ID", "169510539")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from dashboard.office_hourly_routes import office_mobile


def finish(coroutine: Any) -> Any:
    try:
        coroutine.send(None)
    except StopIteration as complete:
        return complete.value
    raise AssertionError("unexpected I/O")


def request() -> Request:
    return Request({"type": "http", "method": "GET", "path": "/office/hourly/mobile",
                    "headers": []})


def test_mobile_denies_before_reading() -> None:
    denied = JSONResponse({"error": "unauthorized"}, status_code=401)
    with patch("dashboard.office_hourly_routes.owner_gate", return_value=denied), patch(
        "dashboard.office_hourly_routes.load_report", AsyncMock()
    ) as load:
        response = finish(office_mobile(request()))
    assert response.status_code == 401
    assert response.headers["cache-control"] == "private, no-store"
    load.assert_not_awaited()


def card(task_id: str, question: Any = None) -> SimpleNamespace:
    return SimpleNamespace(row=SimpleNamespace(
        room="main", task_id=task_id, title="Task " + task_id, next_action="Next"),
        status="in progress", question=question)


def test_mobile_uses_existing_scope_and_prioritizes_questions() -> None:
    question = SimpleNamespace(qid=7, question="Proceed?", status="open")
    report = SimpleNamespace(as_of=datetime(2026, 11, 14),
                             cards=[card("A"), card("B", question)])
    with patch("dashboard.office_hourly_routes.owner_gate", return_value=None), patch(
        "dashboard.office_hourly_routes.load_report", AsyncMock(return_value=report)
    ) as load:
        response = finish(office_mobile(request()))
    load.assert_awaited_once_with(include_closed=False)
    result = json.loads(response.body)
    assert result["schema"] == 1
    assert [task["id"] for task in result["tasks"]] == ["B", "A"]
    assert result["tasks"][0]["question"] == {"id": 7, "text": "Proceed?"}
    assert result["tasks"][1]["question"] is None
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["vary"] == "Cookie"


def test_mobile_applies_redaction_to_all_human_text() -> None:
    report = SimpleNamespace(as_of=datetime(2026, 11, 14),
                             cards=[card("A", SimpleNamespace(qid=1, question="secret", status="open"))])
    with patch("dashboard.office_hourly_routes.owner_gate", return_value=None), patch(
        "dashboard.office_hourly_routes.load_report", AsyncMock(return_value=report)
    ), patch("dashboard.office_hourly_routes.safe_text", return_value="[redacted]") as redact:
        result = json.loads(finish(office_mobile(request())).body)
    assert redact.call_count == 3
    assert result["tasks"][0]["title"] == "[redacted]"
    assert result["tasks"][0]["next_action"] == "[redacted]"
    assert result["tasks"][0]["question"]["text"] == "[redacted]"


def test_mobile_never_returns_answered_question_as_actionable() -> None:
    report = SimpleNamespace(as_of=datetime(2026, 11, 14),
                             cards=[card("A", SimpleNamespace(qid=1, question="old", status="answered"))])
    with patch("dashboard.office_hourly_routes.owner_gate", return_value=None), patch(
        "dashboard.office_hourly_routes.load_report", AsyncMock(return_value=report)
    ):
        result = json.loads(finish(office_mobile(request())).body)
    assert result["tasks"][0]["question"] is None

def test_mobile_answer_guard_is_atomic_and_rejects_answered_question() -> None:
    import pytest
    from vera_shared.room import questions

    session = SimpleNamespace(get=AsyncMock())
    question = SimpleNamespace(status="answered")
    with (
        patch.object(questions, "_question", AsyncMock(return_value=question)),
        pytest.raises(questions.QuestionState, match="no longer open"),
    ):
        finish(questions._answer_in(
            session, "main", "T", 1, "Different answer", "owner", require_open=True))
    session.get.assert_not_awaited()


def test_mobile_answer_forwards_opt_in_guard() -> None:
    from vera_shared.room import questions

    session = SimpleNamespace()
    with patch.object(questions, "_answer_in", AsyncMock(return_value=({}, True))) as inner:
        finish(questions.answer(room="main", task_id="T", qid=1, text=" Yes ",
                                s=session, require_open=True))
    inner.assert_awaited_once_with(session, "main", "T", 1, "Yes", "owner", require_open=True)
