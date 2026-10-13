"""redact_view на витринах: gateway-карточка, карточка и список дашборда, панель графа,
комната агентов. Синтетика CANARY; хранится и сверяется сырое."""
from __future__ import annotations

import base64
import os
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("TOKEN_SECRET", base64.urlsafe_b64encode(b"0" * 32).decode())
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("OWNER_TELEGRAM_ID", "169510539")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import httpx  # noqa: E402
import pytest  # noqa: E402
from dashboard.app import app  # noqa: E402
from dashboard.auth import COOKIE_NAME  # noqa: E402
from dashboard.auth_routes import _set_session_cookie  # noqa: E402
from dashboard.event_view import event_card  # noqa: E402
from dashboard.events_view import events_table  # noqa: E402
from gateway.events import get_event  # noqa: E402
from starlette.responses import Response  # noqa: E402
from vera_shared.db.engine import get_session  # noqa: E402
from vera_shared.db.models import EventRow  # noqa: E402
from vera_shared.graph import panel_events  # noqa: E402
from vera_shared.redact_view import MASK  # noqa: E402

GH = "ghp_" + "CANARY" * 4
UUID = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
TX = "https://my.sepay.vn/transactions/details/85261396"
LEAKY = f"Subject: tx\n---\nкод {UUID} sha {SHA} tx 4930118822771 {TX}\ntoken {GH}\npassword: CANARY-pw-1"
KEEP = (UUID, SHA, "4930118822771", TX)


def clean(blob: str) -> None:
    assert "CANARY" not in blob, blob
    for k in KEEP:
        assert k in blob, k


def cookie() -> dict[str, str]:
    resp = Response()
    _set_session_cookie(resp)
    return {COOKIE_NAME: resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]}


class _Ctx:
    def __init__(self, s):
        self.s = s

    async def __aenter__(self):
        return self.s

    async def __aexit__(self, *a):
        return False


@pytest.mark.asyncio
async def test_gateway_event_masks_text_and_metadata_keeps_ids():
    row = SimpleNamespace(
        id=1, source="gmail", source_event_id="s1", account="a", category="email",
        content_text=LEAKY, occurred_at=datetime(2026, 1, 1), received_at=None,
        triage_status="done", triage_metadata={"note": f"token {GH}", "api_key": "CANARYkey123"},
        importance=5, nature=None, project=None, ready_subtype=None)
    session = MagicMock()

    async def get(*_a):
        return row
    session.get = get
    with patch("gateway.events.get_session", MagicMock(return_value=_Ctx(session))):
        out = await get_event(1, x_internal_secret="test-internal-secret")
    clean(out["content_text"] + str(out["triage_metadata"]))
    assert (out["id"], out["source_event_id"], out["triage_status"]) == (1, "s1", "done")


def _row(**over):
    base = {"id": 7, "source": "gmail", "account": "a", "category": None,
            "occurred_at": datetime(2026, 1, 1, 12), "importance": 1, "nature": None,
            "project": None, "triage_status": "done", "triage_error": f"boom token {GH}",
            "content_text": LEAKY, "metadata": {"window_title": f"x token {GH}"},
            "content_extra": {"kind": "voice_transcript", "chars": 3, "utterances": [
                {"at": 0, "stream": "mic", "text": f"пароль: CANARYvoice99 {UUID}"}]},
            "has_emb": False, "request_id": None, "model": None, "tokens_in": 0,
            "tokens_out": 0, "cost_usd": 0}
    return {**base, **over}


def test_card_masks_body_transcript_and_service_fields():
    html = event_card(_row())
    assert "CANARY" not in html and MASK in html
    for k in KEEP[:2]:
        assert k in html


def test_card_raw_keeps_original_and_has_toggle_back():
    html = event_card(_row(), raw=True)
    assert GH in html and "Скрыть секреты" in html
    assert "?raw=1" in event_card(_row())


def test_list_snippet_masked_lookalikes_intact():
    html = events_table([_row(content_text=f"---\nкод {UUID} tx 4930118822771 token {GH}")],
                        datetime(2026, 1, 1).date(), False)
    assert "CANARY" not in html and UUID in html and "4930118822771" in html


class _Result:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def first(self):
        return self.row


async def _get(path: str, cookies: dict[str, str] | None = None) -> httpx.Response:
    session = MagicMock()

    async def execute(*_a, **_k):
        return _Result(_row())
    session.execute = execute
    with patch("dashboard.events_routes.get_session", MagicMock(return_value=_Ctx(session))), \
         patch("dashboard.events_routes.find_entity_by_alias", AsyncMock(return_value=None)):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t", follow_redirects=False) as c:
            c.cookies.update(cookies or {})
            return await c.get(path)


@pytest.mark.asyncio
async def test_route_owner_default_masked_raw_only_for_owner():
    masked = await _get("/events/7", cookie())
    assert masked.status_code == 200 and "CANARY" not in masked.text
    assert masked.headers["cache-control"] == "no-store"
    raw = await _get("/events/7?raw=1", cookie())
    assert GH in raw.text and raw.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_route_non_owner_never_gets_raw():
    anon = await _get("/events/7?raw=1")
    assert anon.status_code in (303, 401) and "CANARY" not in anon.text
    bad = await _get("/events/7?raw=1", {COOKIE_NAME: "forged"})
    assert "CANARY" not in bad.text and GH not in bad.text


def test_owner_wants_raw_rejects_without_session():
    from dashboard.redaction import owner_wants_raw
    req = SimpleNamespace(query_params={"raw": "1"}, cookies={})
    assert owner_wants_raw(req) is False


@pytest.mark.asyncio
async def test_panel_snippet_and_subject_masked(sqlite_db):
    async with get_session() as s:
        s.add(EventRow(source="telegram", source_event_id="g1", occurred_at=datetime(2026, 1, 1),
                       content_text=f"Author: X\n---\nкод {UUID} token {GH}",
                       metadata_={"sender_id": "42"}))
    events, _ = await panel_events.recent_events_status([("telegram", "user:42")])
    blob = str(events)
    assert "CANARY" not in blob and UUID in blob


@pytest.mark.asyncio
async def test_event_links_preview_masked(sqlite_db):
    from vera_shared.graph import event_links, repo
    ent = await repo.upsert_entity(type="person", name="Zed", source="telegram", identifier="user:42")
    async with get_session() as s:
        s.add(EventRow(source="telegram", source_event_id="l1", occurred_at=datetime(2026, 1, 1),
                       content_text=f"код {UUID} token {GH}", metadata_={"sender_id": "42"}))
    rows = await event_links.timeline_events(ent, datetime(2025, 1, 1), datetime(2027, 1, 1), 5)
    assert rows and "CANARY" not in str(rows) and UUID in str(rows)
