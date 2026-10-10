"""HTTP-фасад office intake: аутентификация сервисным токеном, коды ошибок, секреты не в логах."""
from __future__ import annotations

import logging

import httpx
import pytest
from vera_mcp.auth import OFFICE_PRINCIPAL_KEY, OFFICE_REALM, REALM_SCOPE_KEY
from vera_mcp.office_routes import office_app
from vera_mcp.server import _RealmDispatch, build_app
from vera_shared.office import intake
from vera_shared.office.acl import OfficePrincipal
from vera_shared.room import questions, tasks

OFFICE = "office-vbot-0123456789-0123456789ab"
OFFICE2 = "office-vbot2-0123456789-0123456789a"
ROOM_TOKEN = "room-claude-0123456789-0123456789"
OWNER = "owner-token-0123456789-0123456789"
ROOM = "office-http"
SECRET_BODY = "Закажи воду для кухни, секретная фраза зебра-42"
PAYLOAD = {"client_task_id": "tg--100-55", "title": "Вода", "body": SECRET_BODY,
           "origin": {"channel": "telegram", "chat": "-100", "message": "55"},
           "result_recipient": {"channel": "telegram", "chat": "-100", "reply_to": "55"}}


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def env(monkeypatch, sqlite_db):
    monkeypatch.setenv("OFFICE_ROOM", ROOM)
    monkeypatch.setenv("OFFICE_SERVICE_TOKENS",
                       f"vbot:Veranda:{OFFICE},vbot2:Veranda:{OFFICE2}")
    monkeypatch.setenv("MCP_TOKENS", f"claude:{OWNER}")
    monkeypatch.setenv("ROOM_TOKENS", f"claude:{ROOM_TOKEN}")


@pytest.fixture
async def client(env):
    # без lifespan: офисным маршрутам менеджеры сессий MCP не нужны
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


async def _make(client, **over):
    r = await client.post("/office/v1/tasks", json={**PAYLOAD, **over}, headers=_h(OFFICE))
    assert r.status_code in (200, 201), r.text
    return r.json()


async def _ask(task_id: str) -> tuple[int, int]:
    held = await tasks.claim(room=ROOM, task_id=task_id, agent="claude", lease_seconds=300)
    q = await questions.ask(room=ROOM, task_id=task_id, agent="claude",
                            fencing_token=held["fencing_token"], question="Сколько?")
    from vera_shared.db.engine import get_session
    from vera_shared.office import repository
    async with get_session() as s:
        return q["qid"], await repository.revision_of(s, ROOM, task_id)


@pytest.mark.parametrize(("method", "path", "headers"), [
    ("POST", "/office/v1/tasks", {}),
    ("GET", "/office/v1/tasks", _h("x" * 40)),
    ("GET", "/office/v1/tasks", _h(ROOM_TOKEN)),  # токен комнаты — не сервисный
    ("GET", "/office/v1/tasks", _h(OWNER)),
])
async def test_office_requires_service_token(client, method, path, headers):
    r = await client.request(method, path, headers=headers)
    assert r.status_code == 401 and r.json() == {"error": "unauthorized", "retryable": False}


async def test_service_token_is_rejected_by_mcp_realms(client):
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    hdr = {**_h(OFFICE), "Accept": "application/json, text/event-stream"}
    assert (await client.post("/mcp", json=rpc, headers=hdr)).status_code == 401


async def test_realm_dispatch_never_serves_office_realm():
    sent = []

    async def app(scope, receive, send):  # pragma: no cover - не должен вызываться
        raise AssertionError("service principal reached an MCP realm")

    async def send(message):
        sent.append(message)

    dispatch = _RealmDispatch(app, app)
    await dispatch({"type": "http", REALM_SCOPE_KEY: OFFICE_REALM}, None, send)
    assert sent[0]["status"] == 403


async def test_office_app_without_authenticated_principal_is_403():
    transport = httpx.ASGITransport(app=office_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/tasks")
    assert r.status_code == 403 and r.json()["retryable"] is False


async def test_claim_and_post_are_not_exposed(client):
    made = await _make(client)
    for path in ("claim", "release", "progress", "update"):
        r = await client.post(f"/office/v1/tasks/{made['task_id']}/{path}", json={},
                              headers=_h(OFFICE))
        assert r.status_code in (404, 405)
    assert (await client.post("/office/v1/messages", json={}, headers=_h(OFFICE))
            ).status_code in (404, 405)


async def test_create_idempotent_and_conflict(client):
    first = await client.post("/office/v1/tasks", json=PAYLOAD, headers=_h(OFFICE))
    again = await client.post("/office/v1/tasks", json=PAYLOAD, headers=_h(OFFICE))
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json()["task_id"] == first.json()["task_id"] and not again.json()["created"]
    bad = await client.post("/office/v1/tasks", json={**PAYLOAD, "body": "иначе"},
                            headers=_h(OFFICE))
    assert bad.status_code == 409
    assert bad.json()["error"] == "payload_mismatch" and bad.json()["retryable"] is False


async def test_project_in_body_is_ignored(client):
    made = await _make(client, project="Elsewhere")
    from vera_shared.db.engine import get_session
    from vera_shared.db.models_room import RoomTaskRow
    async with get_session() as s:
        assert (await s.get(RoomTaskRow, (ROOM, made["task_id"]))).project == "Veranda"


async def test_validation_malformed_and_busy_codes(client, monkeypatch):
    nulls = await client.post(
        "/office/v1/tasks", headers=_h(OFFICE),
        json={**PAYLOAD, "origin": {**PAYLOAD["origin"], "thread": None}})
    assert nulls.status_code == 422 and nulls.json()["retryable"] is False
    broken = await client.post("/office/v1/tasks", content=b"{not json", headers=_h(OFFICE))
    assert broken.status_code == 400 and broken.json()["error"] == "malformed_request"
    assert (await client.post("/office/v1/tasks", content=b"[]", headers=_h(OFFICE))
            ).status_code == 400

    async def busy(*_a, **_k):
        raise intake.IntakeBusy("in progress")

    monkeypatch.setattr(intake, "create_task", busy)
    r = await client.post("/office/v1/tasks", json=PAYLOAD, headers=_h(OFFICE))
    assert r.status_code == 503 and r.json()["retryable"] is True
    assert r.headers["retry-after"] == "1"


async def test_answer_flow_revision_and_idempotency(client):
    made = await _make(client)
    qid, rev = await _ask(made["task_id"])
    url = f"/office/v1/tasks/{made['task_id']}/answers"
    body = {"question_id": qid, "text": "Две", "expected_revision": rev,
            "idempotency_key": "k1"}
    stale = await client.post(url, json={**body, "expected_revision": rev - 1},
                              headers=_h(OFFICE))
    assert stale.status_code == 409 and stale.json()["error"] == "revision_mismatch"
    ok = await client.post(url, json=body, headers=_h(OFFICE))
    replay = await client.post(url, json=body, headers=_h(OFFICE))
    assert ok.status_code == replay.status_code == 200
    assert replay.json()["replayed"] and replay.json()["revision"] == ok.json()["revision"]
    other = await client.post(url, json={**body, "text": "Три"}, headers=_h(OFFICE))
    assert other.status_code == 409 and other.json()["error"] == "payload_mismatch"
    typo = await client.post(url, json={**body, "question_id": "1"}, headers=_h(OFFICE))
    assert typo.status_code == 422


async def test_foreign_task_is_404_for_other_principal(client):
    made = await _make(client)
    qid, rev = await _ask(made["task_id"])
    body = {"question_id": qid, "text": "x", "expected_revision": rev, "idempotency_key": "k"}
    r = await client.post(f"/office/v1/tasks/{made['task_id']}/answers", json=body,
                          headers=_h(OFFICE2))
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    r = await client.post(f"/office/v1/tasks/{made['task_id']}/delivered",
                          json={"revision": 1}, headers=_h(OFFICE2))
    assert r.status_code == 404
    assert (await client.get("/office/v1/tasks", headers=_h(OFFICE2))).json()["tasks"] == []


async def test_list_with_string_query_params_and_cursor(client):
    for i in range(3):
        await _make(client, client_task_id=f"tg--100-{i}")
    # SQLite хранит now() без микросекунд и сравнивает строки: равные метки задаём явно
    from datetime import datetime

    from sqlalchemy import update
    from vera_shared.db.engine import get_session
    from vera_shared.db.models_room import RoomTaskRow
    async with get_session() as s:
        await s.execute(update(RoomTaskRow).values(updated_at=datetime(2026, 1, 1)))
    page = await client.get("/office/v1/tasks?limit=2&updated_since=2020-01-01T00:00:00",
                            headers=_h(OFFICE))
    assert page.status_code == 200 and len(page.json()["tasks"]) == 2
    rest = await client.get(f"/office/v1/tasks?limit=20&cursor={page.json()['next_cursor']}",
                            headers=_h(OFFICE))
    assert len(rest.json()["tasks"]) == 1 and rest.json()["next_cursor"] is None
    for query in ("limit=abc", "limit=101", "cursor=@@", "updated_since=вчера"):
        r = await client.get(f"/office/v1/tasks?{query}", headers=_h(OFFICE))
        assert r.status_code == 422, query


async def test_delivered_is_idempotent(client):
    made = await _make(client)
    _, rev = await _ask(made["task_id"])
    url = f"/office/v1/tasks/{made['task_id']}/delivered"
    one = await client.post(url, json={"revision": rev}, headers=_h(OFFICE))
    two = await client.post(url, json={"revision": rev}, headers=_h(OFFICE))
    assert one.json() == two.json() == {"task_id": made["task_id"], "delivered_revision": rev}
    assert (await client.post(url, json={"revision": rev + 9}, headers=_h(OFFICE))
            ).status_code == 409


async def test_token_and_task_text_never_reach_logs(client, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    await _make(client)
    await client.post("/office/v1/tasks", json={**PAYLOAD, "body": SECRET_BODY + "!"},
                      headers=_h(OFFICE))
    await client.get("/office/v1/tasks", headers=_h("wrong-" + OFFICE))

    async def boom(*_a, **_k):
        raise RuntimeError(f"db down {OFFICE} {SECRET_BODY}")

    monkeypatch.setattr(intake, "list_tasks", boom)
    r = await client.get("/office/v1/tasks", headers=_h(OFFICE))
    assert r.status_code == 500 and r.json()["retryable"] is True
    # драйвер БД на DEBUG печатает параметры SQL: это не наш код, в проверку не входит
    ours = "\n".join(r.getMessage() for r in caplog.records
                     if not r.name.startswith(("aiosqlite", "sqlalchemy")))
    assert "office intake failed: RuntimeError" in ours
    assert OFFICE not in ours and "зебра-42" not in ours
    assert OFFICE not in r.text and "зебра-42" not in r.text


def test_principal_key_is_set_only_by_auth():
    assert OFFICE_PRINCIPAL_KEY == "office_principal" and OfficePrincipal("a", "P").type == "service"
