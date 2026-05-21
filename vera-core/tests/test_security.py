"""Smoke tests for security-critical fixes from the May review."""
import pytest

from app.orchestrator.tool_router import _ResolveError, _resolve_safe_args


@pytest.mark.asyncio
async def test_send_reply_requires_email_and_thread():
    with pytest.raises(_ResolveError):
        await _resolve_safe_args("gmail_send_reply", {"to": "victim@x.com"})
    with pytest.raises(_ResolveError):
        await _resolve_safe_args("gmail_send_reply",
                                  {"email": "a@b.c", "to": "victim@x.com"})


@pytest.mark.asyncio
async def test_non_destructive_tools_unchanged():
    args = {"email": "a@b.c", "query": "anything"}
    out = await _resolve_safe_args("gmail_list_threads", args)
    assert out == args


def test_allowlist_blocks_external_host():
    from app.internal.agents import _REGISTRATION_ALLOWED_HOSTS
    assert "vera-telegram" in _REGISTRATION_ALLOWED_HOSTS
    assert "evil.com" not in _REGISTRATION_ALLOWED_HOSTS
    assert "vera-fake" not in _REGISTRATION_ALLOWED_HOSTS
