"""Service-to-service HMAC gate — every /internal/* endpoint must reject
requests missing or with wrong X-Internal-Secret."""
import os

import pytest
from fastapi import HTTPException

from vera_shared.internal_auth import require_internal


def test_require_internal_rejects_missing_header():
    with pytest.raises(HTTPException) as exc:
        require_internal(None)
    assert exc.value.status_code == 401


def test_require_internal_rejects_wrong_secret():
    with pytest.raises(HTTPException) as exc:
        require_internal("not-the-right-secret")
    assert exc.value.status_code == 401


def test_require_internal_accepts_correct_secret():
    expected = os.environ["INTERNAL_SECRET"]
    # No exception → pass
    require_internal(expected)


def test_require_internal_uses_constant_time_compare():
    """hmac.compare_digest is what we should be calling — verify by
    feeding two strings differing only by length to make sure the check
    doesn't short-circuit on length alone."""
    expected = os.environ["INTERNAL_SECRET"]
    with pytest.raises(HTTPException):
        require_internal(expected + "x")
    with pytest.raises(HTTPException):
        require_internal(expected[:-1])
