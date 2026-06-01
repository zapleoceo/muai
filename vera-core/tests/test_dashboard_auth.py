"""Owner session + CSRF gate — admin routes must reject anonymous traffic."""
import pytest
from fastapi import HTTPException

from app.config import get_settings
from app.dashboard.auth import (
    _verify, issue_csrf, issue_session, require_owner,
    verify_telegram_auth,
)


def _make_request(method: str = "GET"):
    """Minimal stand-in for fastapi.Request with the bits require_owner reads."""
    class _R: pass
    r = _R()
    r.method = method
    return r


def test_issue_and_verify_session_roundtrip():
    token, ttl = issue_session()
    assert ttl > 0
    payload = _verify(token, get_settings().session_secret)
    assert payload and payload.startswith("owner:")


def test_verify_rejects_tampered_token():
    token, _ = issue_session()
    head, _, _ = token.partition(".")
    tampered = f"{head}.deadbeef" + "00" * 28
    assert _verify(tampered, get_settings().session_secret) is None


def test_require_owner_blocks_without_cookie():
    with pytest.raises(HTTPException) as exc:
        require_owner(_make_request("GET"), vera_session=None, x_csrf=None)
    assert exc.value.status_code == 401


def test_require_owner_blocks_mutating_request_without_csrf():
    token, _ = issue_session()
    with pytest.raises(HTTPException) as exc:
        require_owner(_make_request("POST"), vera_session=token, x_csrf=None)
    assert exc.value.status_code == 403


def test_require_owner_passes_with_session_and_csrf():
    token, _ = issue_session()
    csrf = issue_csrf(token)
    # GET — csrf not required
    assert require_owner(_make_request("GET"), vera_session=token, x_csrf=None) is True
    # POST — csrf required and matches
    assert require_owner(_make_request("POST"), vera_session=token, x_csrf=csrf) is True


def test_telegram_auth_rejects_missing_hash():
    assert verify_telegram_auth({"id": 1, "auth_date": "1"}) is None


def test_telegram_auth_rejects_invalid_hash():
    assert verify_telegram_auth({
        "id": 1, "auth_date": "1", "hash": "0" * 64,
    }) is None
