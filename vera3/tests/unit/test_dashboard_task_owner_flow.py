"""Реальные запросы к маршрутам владельца поверх сервиса и БД (без моков): recover -> start,
close вопроса, wait/resolve; параметры приходят строками формы."""
from __future__ import annotations

import base64
import os
from datetime import timedelta

os.environ.setdefault("TOKEN_SECRET", base64.urlsafe_b64encode(b"0" * 32).decode())
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("OWNER_TELEGRAM_ID", "169510539")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import httpx  # noqa: E402
import pytest  # noqa: E402
from dashboard.app import app  # noqa: E402
from dashboard.auth import COOKIE_NAME  # noqa: E402
from dashboard.auth_routes import _set_session_cookie  # noqa: E402
from starlette.responses import Response  # noqa: E402
from vera_shared.db.engine import get_session  # noqa: E402
from vera_shared.db.models_room import RoomTaskRow  # noqa: E402
from vera_shared.room import questions, task_wait, tasks  # noqa: E402
from vera_shared.timeutil import utc_naive_now  # noqa: E402

pytestmark = pytest.mark.asyncio
R, T = "main", "F1"


def owner_client() -> httpx.AsyncClient:
    resp = Response()
    _set_session_cookie(resp)
    value = resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                             headers={"origin": "http://testserver"},
                             cookies={COOKIE_NAME: value})


async def row() -> RoomTaskRow:
    async with get_session() as s:
        r = await s.get(RoomTaskRow, (R, T))
        s.expunge(r)
        return r


async def expire() -> None:
    async with get_session() as s:
        (await s.get(RoomTaskRow, (R, T))).lease_until = utc_naive_now() - timedelta(seconds=5)


async def test_recover_then_start_over_http(sqlite_db):
    tok = (await tasks.claim(room=R, task_id=T, agent="claude", lease_seconds=600,
                             session="s1"))["fencing_token"]
    qid = (await questions.ask(room=R, task_id=T, agent="claude", fencing_token=tok,
                               question="?"))["qid"]
    await questions.answer(room=R, task_id=T, qid=qid, text="да")
    async with owner_client() as c:
        base = f"/tasks/{R}/{T}"
        alive = await c.post(f"{base}/recover", data={
            "expected_fencing_token": str(tok), "stop_source": "process_absent_observed",
            "effects": "none_known", "residual_risk_ack": "on"})
        assert alive.status_code == 409 and "held" in alive.text  # аренда жива
        await expire()
        no_ack = await c.post(f"{base}/recover", data={
            "expected_fencing_token": str(tok), "stop_source": "process_absent_observed",
            "effects": "none_known"})
        assert no_ack.status_code == 422 and "residual_risk_ack" in no_ack.text
        ok = await c.post(f"{base}/recover", data={
            "expected_fencing_token": str(tok), "stop_source": "process_absent_observed",
            "effects": "none_known", "residual_risk_ack": "on"})
        assert ok.status_code == 200
        r = await row()
        assert (r.fencing_token, r.recovery_hold, r.status) == (tok + 1, True, "blocked")
        stale = await c.post(f"{base}/start", data={"expected_fencing_token": str(tok)})
        assert stale.status_code == 409
        refuse = await c.post(f"{base}/start", data={
            "expected_fencing_token": str(tok + 1), "status": "open"})
        assert refuse.status_code == 422 and "unresolved_question" in refuse.text
        started = await c.post(f"{base}/start", data={"expected_fencing_token": str(tok + 1)})
        assert started.status_code == 200 and (await row()).recovery_hold is False
    got = await tasks.claim(room=R, task_id=T, agent="codex", lease_seconds=60)
    assert [a["qid"] for a in got["answered_questions"]] == [qid]


async def test_close_question_and_wait_resolve_over_http(sqlite_db):
    tok = (await tasks.claim(room=R, task_id=T, agent="claude", lease_seconds=600)
           )["fencing_token"]
    qid = (await questions.ask(room=R, task_id=T, agent="claude", fencing_token=tok,
                               question="?"))["qid"]
    await task_wait.wait(room=R, task_id=T, agent="claude", fencing_token=tok,
                         until_seconds=3600, reason="жду", decision_owner="owner")
    async with owner_client() as c:
        base = f"/tasks/{R}/{T}"
        form = {"reason": "держатель умер", "expected_fencing_token": str(tok)}
        assert (await c.post(f"{base}/questions/{qid}/close", data=form)).status_code == 409
        await expire()
        assert (await c.post(f"{base}/questions/{qid}/close",
                             data={**form, "reason": " "})).status_code == 422
        assert (await c.post(f"{base}/questions/{qid}/close", data=form)).status_code == 200
        assert (await c.post(f"{base}/questions/{qid}/close", data=form)).status_code == 200
        assert (await c.post(f"{base}/questions/999/close", data=form)).status_code == 404
        assert (await c.post(f"{base}/wait/resolve", data={"reason": "решил"})).status_code == 200
    r = await row()
    assert r.waiting_decision_owner is None and r.status == "blocked"
