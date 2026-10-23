"""POST wait/resolve, questions/{qid}/close, recover, start: ворота владельца, same-origin, коды."""
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
from vera_shared.room.questions import QuestionNotFound, QuestionState  # noqa: E402
from vera_shared.room.recovery import RecoverRefused, UnresolvedQuestion  # noqa: E402
from vera_shared.room.tasks import StaleLease, TaskBusy, TaskNotFound  # noqa: E402

SAME = {"origin": "http://testserver"}
BASE = "/tasks/main/T-1"
# (путь, форма, имя сервисной функции)
CASES = {
    "wait": (f"{BASE}/wait/resolve", {"reason": "решил"}, "owner_resolve_wait"),
    "close": (f"{BASE}/questions/7/close",
              {"reason": "держатель умер", "expected_fencing_token": "3"},
              "owner_close_question"),
    "recover": (f"{BASE}/recover",
                {"expected_fencing_token": "3", "stop_source": "process_absent_observed",
                 "effects": "none_known", "residual_risk_ack": "on"}, "owner_recover"),
    "start": (f"{BASE}/start", {"expected_fencing_token": "4"}, "owner_start"),
}
NAMES = list(CASES)


def cookie() -> dict[str, str]:
    resp = Response()
    _set_session_cookie(resp)
    return {COOKIE_NAME: resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]}


def post(name, headers=None, cookies=None, data=None):
    path, form, _ = CASES[name]
    client = TestClient(app, follow_redirects=False)
    return client.post(path, data=form if data is None else data, headers=headers or {},
                       cookies=cookies or {})


def svc(name):
    return patch(f"dashboard.tasks_owner_routes.{CASES[name][2]}", AsyncMock())


@pytest.mark.parametrize("name", NAMES)
def test_non_owner_and_cross_origin_are_refused_before_anything_is_written(name):
    with svc(name) as op:
        assert post(name, headers=SAME).status_code == 401
        assert post(name, headers={"origin": "https://evil.example"},
                    cookies=cookie()).status_code == 403
        assert post(name, cookies=cookie()).status_code == 403
        op.assert_not_called()


@pytest.mark.parametrize("name", NAMES)
def test_service_errors_map_to_http_codes(name):
    errors = ((TaskNotFound("main", "T-1"), 404), (QuestionNotFound(7), 404),
              (TaskBusy("held"), 409), (StaleLease("stale"), 409),
              (QuestionState("acked"), 409), (RecoverRefused("held", "alive"), 409),
              (UnresolvedQuestion("unresolved_question: x"), 422),
              (ValueError("reason must not be empty"), 422))
    for exc, code in errors:
        with patch(f"dashboard.tasks_owner_routes.{CASES[name][2]}",
                   AsyncMock(side_effect=exc)):
            assert post(name, headers=SAME, cookies=cookie()).status_code == code, exc


@pytest.mark.parametrize("name", NAMES)
def test_missing_required_fields_are_422_and_nothing_is_called(name):
    with svc(name) as op:
        assert post(name, headers=SAME, cookies=cookie(), data={}).status_code == 422
        op.assert_not_called()


def test_wait_resolve_and_close_pass_arguments():
    with svc("wait") as op, patch("dashboard.tasks_routes.load_detail",
                                  AsyncMock(return_value=None)):
        assert post("wait", headers=SAME, cookies=cookie()).status_code == 404  # карточки нет
        op.assert_awaited_once_with("main", "T-1", "решил")
    with svc("close") as op, patch("dashboard.tasks_routes.load_detail",
                                   AsyncMock(return_value=None)):
        post("close", headers=SAME, cookies=cookie())
        op.assert_awaited_once_with("main", "T-1", 7, "держатель умер", 3)


def test_recover_passes_checkbox_and_optional_fields():
    with svc("recover") as op, patch("dashboard.tasks_routes.load_detail",
                                     AsyncMock(return_value=None)):
        post("recover", headers=SAME, cookies=cookie())
        op.assert_awaited_once_with("main", "T-1", 3, stop_source="process_absent_observed",
                                    effects="none_known", residual_risk_ack=True,
                                    run_ref=None, stop_ref=None)
    with svc("recover") as op, patch("dashboard.tasks_routes.load_detail",
                                     AsyncMock(return_value=None)):
        form = {**CASES["recover"][1]}
        form.pop("residual_risk_ack")  # галка не отмечена
        post("recover", headers=SAME, cookies=cookie(), data=form)
        assert op.await_args.kwargs["residual_risk_ack"] is False


def test_start_defaults_and_marked_options():
    with svc("start") as op, patch("dashboard.tasks_routes.load_detail",
                                   AsyncMock(return_value=None)):
        post("start", headers=SAME, cookies=cookie())
        op.assert_awaited_once_with("main", "T-1", 4, status="keep", clear_wait=False,
                                    auto_pickup="keep", reason=None)
        post("start", headers=SAME, cookies=cookie(), data={
            "expected_fencing_token": "4", "status": "open", "clear_wait": "on",
            "auto_pickup": "on", "reason": "ок"})
        assert op.await_args.kwargs == {"status": "open", "clear_wait": True,
                                        "auto_pickup": "on", "reason": "ок"}
