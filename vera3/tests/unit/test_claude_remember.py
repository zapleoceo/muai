"""gateway.claude — /v1/claude/remember endpoint (exact + semantic dedup)."""
# ruff: noqa: I001  # imports intentionally split around env setup
from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

# Set env BEFORE gateway imports — config reads at module load.
os.environ.setdefault("INTERNAL_SECRET", "test-internal-secret")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pydantic  # noqa: E402
import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from gateway.claude import (  # noqa: E402
    SEMANTIC_DEDUP_THRESHOLD,
    SEMANTIC_LOOKBACK_DAYS,
    LLMCallFailed,
    RememberRequest,
    _check_internal_secret,
    _content_hash,
    _cosine,
    _find_semantic_neighbour,
)


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
    assert _cosine(v, v) == pytest.approx(1.0)


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
    # valid kinds
    for k in ("fact", "decision", "todo", "preference"):
        RememberRequest(text="some fact", kind=k)
    # invalid
    with pytest.raises(pydantic.ValidationError):
        RememberRequest(text="some fact", kind="random")


def test_remember_request_min_text_length():
    with pytest.raises(pydantic.ValidationError):
        RememberRequest(text="x")


def test_remember_request_max_text_length():
    with pytest.raises(pydantic.ValidationError):
        RememberRequest(text="x" * 8001)


def test_remember_request_defaults():
    r = RememberRequest(text="hello world")
    assert r.kind == "fact"
    assert r.context is None
    assert r.tags == []


def test_remember_request_max_tags():
    with pytest.raises(pydantic.ValidationError):
        RememberRequest(text="hello world", tags=["t"] * 11)


# ─── Internal secret check ─────────────────────────────────────────────────


def test_check_internal_secret_accepts_correct():
    _check_internal_secret("test-internal-secret")   # no raise


def test_check_internal_secret_rejects_wrong():
    with pytest.raises(HTTPException) as exc:
        _check_internal_secret("wrong")
    assert exc.value.status_code == 401


def test_check_internal_secret_rejects_missing():
    with pytest.raises(HTTPException) as exc:
        _check_internal_secret(None)
    assert exc.value.status_code == 401


# ─── Semantic neighbour finder ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_semantic_neighbour_returns_none_on_embed_fail():
    """If broker is down, semantic check must skip gracefully (return None),
    NOT crash the endpoint — exact dedup still works."""
    with patch("gateway.claude.embed",
               AsyncMock(side_effect=LLMCallFailed("broker down"))):
        result = await _find_semantic_neighbour("hello")
    assert result is None


@pytest.mark.asyncio
async def test_find_semantic_neighbour_returns_none_on_empty_vectors():
    with patch("gateway.claude.embed", AsyncMock(return_value=[])):
        result = await _find_semantic_neighbour("hello")
    assert result is None


# ─── Constants ─────────────────────────────────────────────────────────────


def test_dedup_threshold_is_strict():
    """0.92 chosen to balance 'Дима в Джакарте' / 'Дима живёт в Джакарте'
    (sim ≈ 0.94) against unrelated facts (sim < 0.7)."""
    assert 0.85 <= SEMANTIC_DEDUP_THRESHOLD <= 0.99


def test_lookback_window_one_week():
    assert SEMANTIC_LOOKBACK_DAYS == 7
