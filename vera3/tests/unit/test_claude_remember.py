"""gateway.claude — /v1/claude/remember endpoint (exact + semantic dedup)."""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

# Set env BEFORE gateway imports — config reads at module load.
os.environ.setdefault("INTERNAL_SECRET", "test-internal-secret")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from gateway.claude import _content_hash, _cosine  # noqa: E402


# ─── Pure functions ────────────────────────────────────────────────────────


def test_content_hash_stable():
    """Same input → same hash. Whitespace stripped."""
    assert _content_hash("hello") == _content_hash("hello")
    assert _content_hash("  hello  ") == _content_hash("hello")
    assert _content_hash("hello") != _content_hash("hello!")


def test_content_hash_length_16():
    assert len(_content_hash("any input here")) == 16


def test_content_hash_unicode_safe():
    """Cyrillic + emoji must hash without exception."""
    h = _content_hash("Дима живёт в Джакарте 🌴")
    assert len(h) == 16


def test_cosine_identical_vectors():
    v = [0.1, 0.2, 0.3, 0.4]
    assert _cosine(v, v) == 1.0


def test_cosine_orthogonal_zero():
    a = [1.0, 0.0]
    b = [0.0, 1.0]
    assert _cosine(a, b) == 0.0


def test_cosine_handles_empty():
    assert _cosine([], [1.0, 2.0]) == 0.0
    assert _cosine([1.0], [1.0, 2.0]) == 0.0   # mismatched dims


def test_cosine_handles_zero_vector():
    """Zero vector has zero norm → can't divide by zero."""
    assert _cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


# ─── Schema validation ─────────────────────────────────────────────────────


def test_remember_request_validates_kind():
    from gateway.claude import RememberRequest
    import pydantic
    # valid kinds
    for k in ("fact", "decision", "todo", "preference"):
        RememberRequest(text="some fact", kind=k)
    # invalid
    try:
        RememberRequest(text="x", kind="random")
    except pydantic.ValidationError:
        pass
    else:
        raise AssertionError("kind='random' should have been rejected")


def test_remember_request_min_text_length():
    from gateway.claude import RememberRequest
    import pydantic
    try:
        RememberRequest(text="x")
    except pydantic.ValidationError:
        pass
    else:
        raise AssertionError("text too short should have been rejected")


def test_remember_request_max_text_length():
    from gateway.claude import RememberRequest
    import pydantic
    try:
        RememberRequest(text="x" * 8001)
    except pydantic.ValidationError:
        pass
    else:
        raise AssertionError("text too long should have been rejected")


def test_remember_request_defaults():
    from gateway.claude import RememberRequest
    r = RememberRequest(text="hello world")
    assert r.kind == "fact"
    assert r.context is None
    assert r.tags == []


def test_remember_request_max_tags():
    from gateway.claude import RememberRequest
    import pydantic
    try:
        RememberRequest(text="hello world", tags=["t"] * 11)
    except pydantic.ValidationError:
        pass
    else:
        raise AssertionError("more than 10 tags should have been rejected")


# ─── Internal secret check ─────────────────────────────────────────────────


def test_check_internal_secret_accepts_correct():
    from gateway.claude import _check_internal_secret
    _check_internal_secret("test-internal-secret")   # no raise


def test_check_internal_secret_rejects_wrong():
    from fastapi import HTTPException
    from gateway.claude import _check_internal_secret
    try:
        _check_internal_secret("wrong")
    except HTTPException as e:
        assert e.status_code == 401
    else:
        raise AssertionError("wrong secret should have been rejected")


def test_check_internal_secret_rejects_missing():
    from fastapi import HTTPException
    from gateway.claude import _check_internal_secret
    try:
        _check_internal_secret(None)
    except HTTPException as e:
        assert e.status_code == 401
    else:
        raise AssertionError("missing secret should have been rejected")


# ─── Semantic neighbour finder ─────────────────────────────────────────────


async def test_find_semantic_neighbour_returns_none_on_embed_fail():
    """If broker is down, semantic check must skip gracefully (return None),
    NOT crash the endpoint — exact dedup still works."""
    from gateway.claude import LLMCallFailed, _find_semantic_neighbour
    with patch("gateway.claude.embed", AsyncMock(side_effect=LLMCallFailed("broker down"))):
        result = await _find_semantic_neighbour("hello")
    assert result is None


async def test_find_semantic_neighbour_returns_none_on_empty_vectors():
    from gateway.claude import _find_semantic_neighbour
    with patch("gateway.claude.embed", AsyncMock(return_value=[])):
        result = await _find_semantic_neighbour("hello")
    assert result is None


# ─── Constants ─────────────────────────────────────────────────────────────


def test_dedup_threshold_is_strict():
    """0.92 chosen to balance 'Дима в Джакарте' / 'Дима живёт в Джакарте'
    (sim ≈ 0.94) against unrelated facts (sim < 0.7)."""
    from gateway.claude import SEMANTIC_DEDUP_THRESHOLD
    assert 0.85 <= SEMANTIC_DEDUP_THRESHOLD <= 0.99


def test_lookback_window_one_week():
    from gateway.claude import SEMANTIC_LOOKBACK_DAYS
    assert SEMANTIC_LOOKBACK_DAYS == 7
