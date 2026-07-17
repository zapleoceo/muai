"""Security pack: bot fail-closed owner gate, gateway body-size middleware
(chunked bypass), brain-search internal secret, /graph XSS escaping."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

# ─── bot: _owner_only fail-closed ───────────────────────────────────────────


def _msg(from_id: int | None):
    return SimpleNamespace(
        from_user=None if from_id is None else SimpleNamespace(id=from_id))


def test_owner_only_denies_all_when_owner_unset(monkeypatch):
    import bot_telegram.bot as bot_mod
    monkeypatch.setattr(bot_mod, "OWNER_ID", 0)
    assert bot_mod._owner_only(_msg(169510539)) is False
    assert bot_mod._owner_only(_msg(12345)) is False


def test_owner_only_matches_owner_id(monkeypatch):
    import bot_telegram.bot as bot_mod
    monkeypatch.setattr(bot_mod, "OWNER_ID", 169510539)
    assert bot_mod._owner_only(_msg(169510539)) is True
    assert bot_mod._owner_only(_msg(12345)) is False
    assert bot_mod._owner_only(_msg(None)) is False


# ─── gateway: MaxBodySizeMiddleware ─────────────────────────────────────────


def _req(method: str, headers: dict[str, str]):
    return SimpleNamespace(method=method, headers=headers)


@pytest.mark.asyncio
async def test_middleware_rejects_chunked_post_without_length():
    from gateway.app import MaxBodySizeMiddleware
    mw = MaxBodySizeMiddleware(app=None)
    call_next = AsyncMock()
    resp = await mw.dispatch(_req("POST", {}), call_next)
    assert resp.status_code == 411
    call_next.assert_not_awaited()


@pytest.mark.asyncio
async def test_middleware_allows_get_without_length():
    from gateway.app import MaxBodySizeMiddleware
    mw = MaxBodySizeMiddleware(app=None)
    call_next = AsyncMock(return_value="ok")
    assert await mw.dispatch(_req("GET", {}), call_next) == "ok"


@pytest.mark.asyncio
async def test_middleware_rejects_oversize_and_garbage_length():
    from gateway.app import MAX_BODY_BYTES, MaxBodySizeMiddleware
    mw = MaxBodySizeMiddleware(app=None)
    call_next = AsyncMock()
    big = await mw.dispatch(
        _req("POST", {"content-length": str(MAX_BODY_BYTES + 1)}), call_next)
    assert big.status_code == 413
    bad = await mw.dispatch(
        _req("POST", {"content-length": "not-a-number"}), call_next)
    assert bad.status_code == 400
    call_next.assert_not_awaited()


@pytest.mark.asyncio
async def test_middleware_passes_normal_post():
    from gateway.app import MaxBodySizeMiddleware
    mw = MaxBodySizeMiddleware(app=None)
    call_next = AsyncMock(return_value="ok")
    assert await mw.dispatch(
        _req("POST", {"content-length": "512"}), call_next) == "ok"


# ─── brain-search: internal secret fail-closed ──────────────────────────────


def test_search_secret_fail_closed(monkeypatch):
    from brain_search.app import check_internal_secret
    monkeypatch.setenv("INTERNAL_SECRET", "s3cret")
    check_internal_secret("s3cret")                    # не бросает
    with pytest.raises(HTTPException):
        check_internal_secret("wrong")
    with pytest.raises(HTTPException):
        check_internal_secret(None)
    # секрет не сконфигурирован → закрыто для всех, а не открыто
    monkeypatch.setenv("INTERNAL_SECRET", "")
    with pytest.raises(HTTPException):
        check_internal_secret("anything")


# ─── dashboard: /graph экранирует внешние строки ────────────────────────────


def test_graph_page_escapes_untrusted_html():
    from dashboard.graph_routes import _GRAPH_BODY
    assert "function esc(" in _GRAPH_BODY
    assert "esc(labels[c])" in _GRAPH_BODY
    assert "esc(f?f.name" in _GRAPH_BODY
    assert "esc(f.username)" in _GRAPH_BODY
