"""gateway.query — vera-mcp read-side proxies (/v1/search, /v1/events/recent,
/v1/entity/context). DB and brain-search calls are mocked at the session/
client boundary — same style as test_claude_remember.py's semantic-dedup
tests; real DB behavior for graph tables is integration-tested (see
test_graph_repo.py's docstring)."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pydantic
import pytest
from fastapi import HTTPException
from gateway.query import (
    RECENT_EVENTS_LIMIT,
    RELATIONSHIPS_LIMIT,
    SearchProxyRequest,
    _check_internal_secret,
    entity_context,
    recent_events,
    search_proxy,
)


class _FakeSessionCtx:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


# ─── Internal secret check ─────────────────────────────────────────────────


def test_check_internal_secret_accepts_correct():
    _check_internal_secret("test-internal-secret")   # no raise


def test_check_internal_secret_rejects_wrong():
    with pytest.raises(HTTPException) as exc:
        _check_internal_secret("wrong")
    assert exc.value.status_code == 401


# ─── Constants ──────────────────────────────────────────────────────────────


def test_recent_events_limit_is_positive():
    assert RECENT_EVENTS_LIMIT > 0


def test_relationships_limit_is_positive():
    assert RELATIONSHIPS_LIMIT > 0


# ─── SearchProxyRequest validation ──────────────────────────────────────────


def test_search_proxy_request_defaults():
    r = SearchProxyRequest(q="hello")
    assert r.limit == 15
    assert r.use_agent is False


def test_search_proxy_request_limit_bounds():
    with pytest.raises(pydantic.ValidationError):
        SearchProxyRequest(q="hello", limit=0)
    with pytest.raises(pydantic.ValidationError):
        SearchProxyRequest(q="hello", limit=51)


def test_search_proxy_request_min_query_length():
    with pytest.raises(pydantic.ValidationError):
        SearchProxyRequest(q="")


# ─── search_proxy ────────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status_code, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text

    def json(self):
        return self._json


class _FakeAsyncClient:
    def __init__(self, response=None, raise_error=None):
        self._response = response
        self._raise_error = raise_error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        if self._raise_error:
            raise self._raise_error
        return self._response


@pytest.mark.asyncio
async def test_search_proxy_forwards_and_returns_json():
    fake_resp = _FakeResponse(200, json_data={"answer": "", "results": []})
    with patch("gateway.query.httpx.AsyncClient",
               MagicMock(return_value=_FakeAsyncClient(response=fake_resp))):
        result = await search_proxy(
            SearchProxyRequest(q="Stepan", use_agent=False),
            x_internal_secret="test-internal-secret",
        )
    assert result == {"answer": "", "results": []}


@pytest.mark.asyncio
async def test_search_proxy_raises_502_on_connection_error():
    with patch("gateway.query.httpx.AsyncClient",
               MagicMock(return_value=_FakeAsyncClient(
                   raise_error=httpx.ConnectError("down")))), \
         pytest.raises(HTTPException) as exc:
        await search_proxy(SearchProxyRequest(q="x"),
                            x_internal_secret="test-internal-secret")
    assert exc.value.status_code == 502


@pytest.mark.asyncio
async def test_search_proxy_forwards_upstream_error_status():
    fake_resp = _FakeResponse(404, text="not found")
    with patch("gateway.query.httpx.AsyncClient",
               MagicMock(return_value=_FakeAsyncClient(response=fake_resp))), \
         pytest.raises(HTTPException) as exc:
        await search_proxy(SearchProxyRequest(q="x"),
                            x_internal_secret="test-internal-secret")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_search_proxy_rejects_bad_secret():
    with pytest.raises(HTTPException) as exc:
        await search_proxy(SearchProxyRequest(q="x"), x_internal_secret="wrong")
    assert exc.value.status_code == 401


# ─── recent_events ───────────────────────────────────────────────────────────


def _events_session(rows):
    session = MagicMock()
    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = rows
    session.execute = AsyncMock(return_value=execute_result)
    return session


@pytest.mark.asyncio
async def test_recent_events_shapes_response():
    row = SimpleNamespace(
        id=1, source="manual", account=None,
        occurred_at=datetime(2026, 7, 3, 12, 0, 0),
        content_text="x" * 500,
        importance=80, project="itstep",
    )
    session = _events_session([row])
    with patch("gateway.query.get_session",
               MagicMock(return_value=_FakeSessionCtx(session))):
        result = await recent_events(hours=24, source=None,
                                      x_internal_secret="test-internal-secret")
    assert result["count"] == 1
    assert result["truncated"] is False
    ev = result["events"][0]
    assert ev["id"] == 1
    assert len(ev["content_preview"]) == 300   # truncated to 300 chars
    assert ev["importance"] == 80
    assert ev["project"] == "itstep"


@pytest.mark.asyncio
async def test_recent_events_truncated_flag_when_at_cap():
    row = SimpleNamespace(
        id=1, source="manual", account=None,
        occurred_at=datetime(2026, 7, 3), content_text="hi",
        importance=None, project=None,
    )
    rows = [row] * RECENT_EVENTS_LIMIT
    session = _events_session(rows)
    with patch("gateway.query.get_session",
               MagicMock(return_value=_FakeSessionCtx(session))):
        result = await recent_events(hours=1, source=None,
                                      x_internal_secret="test-internal-secret")
    assert result["truncated"] is True


@pytest.mark.asyncio
async def test_recent_events_empty_result():
    session = _events_session([])
    with patch("gateway.query.get_session",
               MagicMock(return_value=_FakeSessionCtx(session))):
        result = await recent_events(hours=24, source="gmail",
                                      x_internal_secret="test-internal-secret")
    assert result == {"count": 0, "truncated": False, "events": []}


@pytest.mark.asyncio
async def test_recent_events_rejects_bad_secret():
    with pytest.raises(HTTPException) as exc:
        await recent_events(hours=24, source=None, x_internal_secret="wrong")
    assert exc.value.status_code == 401


# ─── entity_context ──────────────────────────────────────────────────────────


def _context_session(entity, rel_rows):
    session = MagicMock()
    session.get = AsyncMock(return_value=entity)
    execute_result = MagicMock()
    execute_result.mappings.return_value.all.return_value = rel_rows
    session.execute = AsyncMock(return_value=execute_result)
    return session


@pytest.mark.asyncio
async def test_entity_context_composes_full_response():
    entity = SimpleNamespace(
        id=42, name="Дмитрий Егоров", type="person",
        canonical_id=None, attributes={},
        last_seen_at=datetime(2026, 7, 3),
    )
    rel_rows = [{"predicate": "coworker_of", "fact": None, "confidence": 0.8,
                 "direction": "out", "other_name": "Марина", "other_type": "person"}]
    session = _context_session(entity, rel_rows)
    ctx = {"aliases": [{"source": "telegram", "identifier": "1"}],
           "memberships": [], "recent_30d_messages": 3}
    with patch("gateway.query.get_session",
               MagicMock(return_value=_FakeSessionCtx(session))), \
         patch("gateway.query.find_entity_by_name", AsyncMock(return_value=42)), \
         patch("gateway.query.get_entity_context", AsyncMock(return_value=ctx)), \
         patch("gateway.query.list_members", AsyncMock(return_value=[])):
        result = await entity_context(name="Дмитрий",
                                       x_internal_secret="test-internal-secret")
    assert result["entity_id"] == 42
    assert result["name"] == "Дмитрий Егоров"
    assert result["recent_30d_messages"] == 3
    assert result["relationships"][0]["predicate"] == "coworker_of"
    assert result["aliases"] == ctx["aliases"]


@pytest.mark.asyncio
async def test_entity_context_404_when_not_found():
    with patch("gateway.query.find_entity_by_name", AsyncMock(return_value=None)), \
         pytest.raises(HTTPException) as exc:
        await entity_context(name="Unknown Person",
                              x_internal_secret="test-internal-secret")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_entity_context_rejects_bad_secret():
    with pytest.raises(HTTPException) as exc:
        await entity_context(name="X", x_internal_secret="wrong")
    assert exc.value.status_code == 401
