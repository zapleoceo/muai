"""gateway.claude — /v1/claude/remember endpoint (exact + semantic dedup)."""
# ruff: noqa: I001  # imports intentionally split around env setup
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

# Set env BEFORE gateway imports — config reads at module load.
os.environ.setdefault("INTERNAL_SECRET", "test-internal-secret")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pydantic  # noqa: E402
import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from gateway.auth import check_internal_secret  # noqa: E402
from gateway.claude import RememberRequest  # noqa: E402
from vera_shared.llm.client import LLMCallFailed  # noqa: E402
from vera_shared.memory.remember import (  # noqa: E402
    SEMANTIC_DEDUP_THRESHOLD,
    SEMANTIC_LOOKBACK_DAYS,
    _content_hash,
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
    check_internal_secret("test-internal-secret")   # no raise


def test_check_internal_secret_rejects_wrong():
    with pytest.raises(HTTPException) as exc:
        check_internal_secret("wrong")
    assert exc.value.status_code == 401


def test_check_internal_secret_rejects_missing():
    with pytest.raises(HTTPException) as exc:
        check_internal_secret(None)
    assert exc.value.status_code == 401


def test_check_internal_secret_fails_closed_when_unconfigured(monkeypatch):
    """Security fix: an empty/unset INTERNAL_SECRET used to fail OPEN
    (any caller, even with no header, passed). Must now reject everyone —
    a misconfigured deployment should be locked down, not wide open."""
    from gateway import config as gw_config
    monkeypatch.setattr(gw_config, "_settings", None)
    monkeypatch.setenv("INTERNAL_SECRET", "")
    with pytest.raises(HTTPException) as exc:
        check_internal_secret(None)
    assert exc.value.status_code == 401
    with pytest.raises(HTTPException):
        check_internal_secret("anything")


# ─── Semantic neighbour finder ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_semantic_neighbour_returns_none_on_embed_fail():
    """If broker is down, semantic check must skip gracefully (return None),
    NOT crash the endpoint — exact dedup still works."""
    with patch("vera_shared.memory.remember.embed",
               AsyncMock(side_effect=LLMCallFailed("broker down"))):
        result = await _find_semantic_neighbour("hello")
    assert result == (None, None)


@pytest.mark.asyncio
async def test_find_semantic_neighbour_returns_none_on_empty_vectors():
    with patch("vera_shared.memory.remember.embed", AsyncMock(return_value=[])):
        result = await _find_semantic_neighbour("hello")
    assert result == (None, None)


class _FakeSessionCtx:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


def _db_session(best):
    """Единственный execute — ближайший по halfvec (.first())."""
    result = MagicMock()
    result.first.return_value = best
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)
    return session


async def _neighbour(session, vector=None):
    with patch("vera_shared.memory.remember.embed",
               AsyncMock(return_value=[vector or [1.0, 0.0]])),          patch("vera_shared.memory.remember.get_session",
               MagicMock(return_value=_FakeSessionCtx(session))):
        return await _find_semantic_neighbour("hello")


@pytest.mark.asyncio
async def test_find_semantic_neighbour_takes_the_database_cosine():
    q_vec, match = await _neighbour(_db_session((2, 0.97)))
    assert q_vec == [1.0, 0.0]                     # вектор отдаётся для записи
    assert match == (2, pytest.approx(0.97))


@pytest.mark.asyncio
async def test_find_semantic_neighbour_none_when_below_threshold():
    q_vec, match = await _neighbour(_db_session((1, 0.10)))
    assert q_vec == [1.0, 0.0]   # даже без матча вектор идёт в event_embeddings
    assert match is None


@pytest.mark.asyncio
async def test_find_semantic_neighbour_none_when_no_candidates():
    _q, match = await _neighbour(_db_session(None))
    assert match is None


@pytest.mark.asyncio
async def test_semantic_dedup_queries_only_the_vector_column():
    """Без JSONB-перебора: один запрос по halfvec. Каст запроса — без
    размерности: pgvector сверяет её с колонкой при сравнении. 13.09.2026
    зашитая `halfvec(1024)` уронила CI — интеграционный тест держит колонку
    `halfvec(3)`, и сравнение отказалось работать."""
    session = _db_session((1, 0.97))
    await _neighbour(session)
    assert session.execute.await_count == 1
    sql = str(session.execute.await_args_list[0].args[0])
    assert "CAST(:q AS halfvec)" in sql and "halfvec(" not in sql
    assert "ee.embedding," not in sql and "jsonb" not in sql.lower()


# ─── Constants ─────────────────────────────────────────────────────────────


def test_dedup_threshold_is_strict():
    """0.92 chosen to balance 'Дима в Джакарте' / 'Дима живёт в Джакарте'
    (sim ≈ 0.94) against unrelated facts (sim < 0.7)."""
    assert 0.85 <= SEMANTIC_DEDUP_THRESHOLD <= 0.99


def test_lookback_window_one_week():
    assert SEMANTIC_LOOKBACK_DAYS == 7
