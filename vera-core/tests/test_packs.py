"""Coverage for Pack S/R/N changes."""
import pytest

from app.mcp.manager import _looks_like_auth_error
from app.orchestrator.tool_router import (
    AUTO_SAFE_TOOLS, DESTRUCTIVE_TOOLS, _ResolveError, _resolve_safe_args,
    is_auto_safe,
)


def test_auth_pattern_does_not_false_positive_on_content():
    assert not _looks_like_auth_error("the user said unauthorized words")
    assert not _looks_like_auth_error("page returned 'permission denied' as text body")


def test_auth_pattern_catches_real_errors():
    assert _looks_like_auth_error("HTTP 401 Unauthorized")
    assert _looks_like_auth_error("403 Forbidden")
    assert _looks_like_auth_error("invalid_grant")
    assert _looks_like_auth_error("Token has expired")
    assert _looks_like_auth_error("Authentication failed: bad signature")


def test_auto_whitelist_excludes_destructive_send():
    assert not is_auto_safe("gmail_send_reply")
    assert not is_auto_safe("telegram_send_message")
    assert is_auto_safe("gmail_modify_thread")
    assert is_auto_safe("telegram_read_messages")


def test_destructive_set_intersects_correctly():
    # Modify-style tools are both destructive AND auto-safe (idempotent labels)
    overlap = DESTRUCTIVE_TOOLS & AUTO_SAFE_TOOLS
    assert "gmail_modify_thread" in overlap
    # but pure send tools never overlap
    assert "telegram_send_message" not in AUTO_SAFE_TOOLS


@pytest.mark.asyncio
async def test_telegram_send_requires_explicit_peer():
    with pytest.raises(_ResolveError):
        await _resolve_safe_args("telegram_send_message", {"text": "hi"})
    with pytest.raises(_ResolveError):
        await _resolve_safe_args("telegram_send_reaction", {"emoji": "👍"})


@pytest.mark.asyncio
async def test_telegram_send_accepts_explicit_peer():
    out = await _resolve_safe_args("telegram_send_message",
                                    {"peer": "@alice", "text": "hi"})
    assert out["peer"] == "@alice"
    out = await _resolve_safe_args("telegram_send_reaction",
                                    {"chat_id": 123, "emoji": "👍"})
    assert out["chat_id"] == 123


@pytest.mark.asyncio
async def test_telegram_send_strips_from_override():
    out = await _resolve_safe_args("telegram_send_message",
                                    {"peer": "@alice", "text": "hi",
                                     "from": "spoof@evil"})
    assert "from" not in out


def test_filter_engine_warns_on_unknown_predicate(caplog):
    from vera_shared.sources import filters
    filters._warned.clear()
    caplog.set_level("WARNING")
    rules = [{"match": {"nonexistent_key": "value"}, "action": "include"}]
    result = filters.evaluate(rules, {"chat_type": "private"})
    assert result == "exclude"
    assert any("unknown predicate" in r.message for r in caplog.records)
