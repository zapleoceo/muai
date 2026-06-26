"""Тесты статус-переходов Gmail-поллера: revoked → needs_reauth, ok → clear."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(__file__), "..", "..",
    "services", "ingestor-gmail", "src"))

os.environ.setdefault("GMAIL_CLIENT_ID", "test-cid")
os.environ.setdefault("GMAIL_CLIENT_SECRET", "test-csec")

from ingestor_gmail import poller  # noqa: E402


class _FakeResp:
    def __init__(self, status_code, text="", json_data=None):
        self.status_code = status_code
        self.text = text
        self._json = json_data or {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **kw):
        return self._resp

    async def get(self, *a, **kw):
        return self._resp


@pytest.mark.asyncio
async def test_invalid_grant_raises_token_revoked(monkeypatch):
    monkeypatch.setattr(
        poller.httpx, "AsyncClient",
        lambda *a, **kw: _FakeClient(_FakeResp(
            400, text='{"error":"invalid_grant","error_description":"revoked"}')),
    )
    with pytest.raises(poller.TokenRevoked):
        await poller.refresh_access("1//dead-token")


@pytest.mark.asyncio
async def test_other_400_not_token_revoked(monkeypatch):
    # 400 без invalid_grant (напр. invalid_client) — НЕ TokenRevoked,
    # это другой класс ошибки (не помечаем needs_reauth зря)
    monkeypatch.setattr(
        poller.httpx, "AsyncClient",
        lambda *a, **kw: _FakeClient(_FakeResp(
            400, text='{"error":"invalid_client"}')),
    )
    with pytest.raises(Exception) as ei:
        await poller.refresh_access("1//tok")
    assert not isinstance(ei.value, poller.TokenRevoked)


@pytest.mark.asyncio
async def test_success_returns_tokens(monkeypatch):
    monkeypatch.setattr(
        poller.httpx, "AsyncClient",
        lambda *a, **kw: _FakeClient(_FakeResp(
            200, json_data={"access_token": "ya29.xxx", "expires_in": 3599})),
    )
    tok = await poller.refresh_access("1//good")
    assert tok["access_token"] == "ya29.xxx"


@pytest.mark.asyncio
async def test_fetch_403_insufficient_scope_raises(monkeypatch):
    # Сценарий demoniwwwe: токен валиден, но без gmail-scope → 403
    monkeypatch.setattr(
        poller.httpx, "AsyncClient",
        lambda *a, **kw: _FakeClient(_FakeResp(
            403, text='{"error":{"code":403,"message":"Insufficient Permission"}}')),
    )
    with pytest.raises(poller.ScopeInsufficient):
        await poller.fetch_messages("ya29.scopeless", "newer_than:7d")


@pytest.mark.asyncio
async def test_fetch_other_403_not_scope(monkeypatch):
    # 403 без "insufficient" (напр. rate/quota) — НЕ ScopeInsufficient
    monkeypatch.setattr(
        poller.httpx, "AsyncClient",
        lambda *a, **kw: _FakeClient(_FakeResp(
            403, text='{"error":{"code":403,"message":"userRateLimitExceeded"}}')),
    )
    with pytest.raises(Exception) as ei:
        await poller.fetch_messages("ya29.x", "q")
    assert not isinstance(ei.value, poller.ScopeInsufficient)
