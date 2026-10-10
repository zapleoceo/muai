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


def _callback(from_id: int | None):
    message = SimpleNamespace(edit_reply_markup=AsyncMock())
    return SimpleNamespace(
        from_user=None if from_id is None else SimpleNamespace(id=from_id),
        data="vh:y:vc-0123456789abcdef", message=message, answer=AsyncMock())


def _as_message(monkeypatch, bot_mod):
    # Хендлер снимает кнопки только с настоящего Message — подставка проходит.
    monkeypatch.setattr(bot_mod, "Message", SimpleNamespace)


@pytest.mark.asyncio
@pytest.mark.parametrize(("owner", "presser"), [
    (169510539, 12345), (169510539, None), (0, 169510539), (0, 0),
])
async def test_help_callback_ignores_anyone_but_owner(monkeypatch, owner, presser):
    import bot_telegram.bot as bot_mod
    monkeypatch.setattr(bot_mod, "OWNER_ID", owner)
    _as_message(monkeypatch, bot_mod)
    callback = _callback(presser)
    await bot_mod.on_help_confirmation(callback)
    callback.answer.assert_awaited_once_with()
    callback.message.edit_reply_markup.assert_not_awaited()


@pytest.mark.asyncio
async def test_help_callback_from_owner_only_explains(monkeypatch):
    import bot_telegram.bot as bot_mod
    monkeypatch.setattr(bot_mod, "OWNER_ID", 169510539)
    _as_message(monkeypatch, bot_mod)
    callback = _callback(169510539)
    await bot_mod.on_help_confirmation(callback)
    callback.answer.assert_awaited_once_with(bot_mod.STALE_BUTTON_TEXT)
    callback.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)


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
    from dashboard.graph_script import GRAPH_SCRIPT
    from dashboard.graph_script_connections import CONNECTIONS_SCRIPT
    # Связи в карточке рисует отдельный модуль, склеенный с GRAPH_SCRIPT в один <script>.
    script = GRAPH_SCRIPT + CONNECTIONS_SCRIPT
    assert "function esc(" in script
    assert "esc(labels[c])" in script
    for field in ("p.name", "c.other_name", "h.name", "e.snippet", "p.username", "p.email"):
        assert f"esc({field})" in script
