"""Тесты подписи OAuth-state (anti-CSRF для Gmail re-auth)."""
from __future__ import annotations

import importlib
import os
import sys
import time

import pytest


@pytest.fixture
def auth(monkeypatch):
    monkeypatch.setenv("TOKEN_SECRET", "oauth-state-test-secret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:tok")
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "169510539")
    sys.path.insert(0, os.path.join(
        os.path.dirname(__file__), "..", "..",
        "services", "dashboard", "src"))
    if "dashboard.auth" in sys.modules:
        return importlib.reload(sys.modules["dashboard.auth"])
    return importlib.import_module("dashboard.auth")


def test_state_round_trip(auth):
    assert auth.verify_oauth_state(auth.issue_oauth_state()) is True


def test_state_tampered_rejected(auth):
    st = auth.issue_oauth_state()
    payload, _ = st.rsplit(".", 1)
    assert auth.verify_oauth_state(f"{payload}.{'0'*64}") is False


def test_state_wrong_prefix_rejected(auth):
    import hashlib, hmac
    bad = "session:" + str(int(time.time()))
    sig = hmac.new(b"oauth-state-test-secret", bad.encode(), hashlib.sha256).hexdigest()
    # подпись валидна, но префикс не gmailoauth → отказ (нельзя переиспользовать
    # session-куку как oauth-state)
    assert auth.verify_oauth_state(f"{bad}.{sig}") is False


def test_state_expired_rejected(auth):
    import hashlib, hmac
    old = f"gmailoauth:{int(time.time()) - 700}"  # > 600s TTL
    sig = hmac.new(b"oauth-state-test-secret", old.encode(), hashlib.sha256).hexdigest()
    assert auth.verify_oauth_state(f"{old}.{sig}") is False


def test_state_none_and_garbage(auth):
    assert auth.verify_oauth_state(None) is False
    assert auth.verify_oauth_state("nodot") is False
    assert auth.verify_oauth_state("") is False
