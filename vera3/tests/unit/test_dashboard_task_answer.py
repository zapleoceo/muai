"""POST /tasks/{room}/{task_id}/answer: ворота владельца и same-origin, экранирование, ошибки."""
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

from dashboard.app import app  # noqa: E402
from dashboard.auth import COOKIE_NAME  # noqa: E402
from dashboard.auth_routes import _set_session_cookie  # noqa: E402
from dashboard.tasks_questions_view import questions_block  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.responses import Response  # noqa: E402
from vera_shared.room.questions import QuestionNotFound, QuestionState  # noqa: E402

AT = datetime(2026, 10, 9, 12, 0)
URL = "/tasks/main/T-1/answer"
SAME = {"origin": "http://testserver"}


def cookie() -> dict[str, str]:
    resp = Response()
    _set_session_cookie(resp)
    return {COOKIE_NAME: resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]}


def question(status="open", text="Вопрос?", qid=7):
    return SimpleNamespace(qid=qid, room="main", task_id="T-1", asked_by="claude",
                           question=text, asked_at=AT, status=status)


def answer_row(text="Да"):
    return SimpleNamespace(text=text, answered_by="owner", answered_at=AT)


def post(headers=None, cookies=None, data=None):
    client = TestClient(app, follow_redirects=False)
    return client.post(URL, data=data or {"qid": "7", "text": "Да"},
                       headers=headers or {}, cookies=cookies or {})


def test_non_owner_is_refused_before_anything_is_written():
    with patch("dashboard.tasks_routes.submit_answer", AsyncMock()) as sub:
        assert post(headers=SAME).status_code == 401
        sub.assert_not_called()


def test_cross_origin_owner_post_is_refused():
    with patch("dashboard.tasks_routes.submit_answer", AsyncMock()) as sub:
        evil = post(headers={"origin": "https://evil.example"}, cookies=cookie())
        assert evil.status_code == 403
        assert post(cookies=cookie()).status_code == 403  # ни Origin, ни Sec-Fetch-Site
        sub.assert_not_called()


def test_owner_same_origin_submits_and_gets_fragment():
    c = cookie()
    with patch("dashboard.tasks_routes.submit_answer", AsyncMock()) as sub, \
            patch("dashboard.tasks_routes.load_detail", AsyncMock(return_value=None)):
        r = post(headers=SAME, cookies=c)
        sub.assert_awaited_once_with("main", "T-1", 7, "Да")
        assert r.status_code == 404  # карточка не найдена -> 404, ответ уже записан


def test_service_errors_map_to_http_codes():
    c = cookie()
    for exc, code in ((QuestionNotFound(7), 404), (QuestionState("closed"), 409),
                      (ValueError("empty"), 422)):
        with patch("dashboard.tasks_routes.submit_answer", AsyncMock(side_effect=exc)):
            assert post(headers=SAME, cookies=c).status_code == code


def test_questions_block_escapes_and_shows_form_only_when_answerable():
    evil = "<script>x</script>"
    html = questions_block([(question(text=evil), [answer_row(evil)])])
    assert "<script>x" not in html and "&lt;script&gt;" in html
    assert 'hx-post="/tasks/main/T-1/answer"' in html and 'name="qid" value="7"' in html
    done = questions_block([(question("acked"), [answer_row()]), (question("withdrawn", qid=8), [])])
    assert "<form" not in done and "ответ принят" in done and "снят" in done
    answered = questions_block([(question("answered"), [answer_row()])])
    assert "<form" in answered and "ждёт исполнителя" in answered
    assert questions_block([]) == ""
