"""Общие части MCP: скрытые события, время, запуск, фильтры поиска."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from vera_shared.events.visibility import (
    DEFAULT_RESTORE_STATUS,
    HIDDEN_STATUS,
    NOT_HIDDEN_SQL,
    PREV_STATUS_KEY,
    hide_values,
    unhide_values,
)
from vera_shared.timeutil import parse_iso_naive


def test_hide_values_remember_previous_status():
    status, meta = hide_values("media_pending", {"k": 1})
    assert status == HIDDEN_STATUS
    assert meta == {"k": 1, PREV_STATUS_KEY: "media_pending"}


def test_hide_values_is_idempotent():
    _, once = hide_values("done", None)
    status, twice = hide_values(HIDDEN_STATUS, once)
    assert (status, twice) == (HIDDEN_STATUS, once)


def test_unhide_values_restores_or_defaults():
    assert unhide_values({PREV_STATUS_KEY: "pending", "k": 1}) == ("pending", {"k": 1})
    assert unhide_values({PREV_STATUS_KEY: "pending"}) == ("pending", None)
    assert unhide_values(None) == (DEFAULT_RESTORE_STATUS, None)


def test_parse_iso_naive_variants():
    assert parse_iso_naive("2026-03-04") == datetime(2026, 3, 4)
    assert parse_iso_naive("2026-03-04T05:06:07") == datetime(2026, 3, 4, 5, 6, 7)
    assert parse_iso_naive("2026-03-04T05:06:07Z") == datetime(2026, 3, 4, 5, 6, 7)
    assert parse_iso_naive(" 2026-03-04T07:06:07+02:00 ") == datetime(2026, 3, 4, 5, 6, 7)


def test_search_filters_exclude_hidden_events():
    from brain_search import retrieval

    assert NOT_HIDDEN_SQL in retrieval._NOT_A_WORLD_EVENT
    assert NOT_HIDDEN_SQL in retrieval.semantic_filter(None, None)[0]
    project = SimpleNamespace(name="p", account_like=[], chats=[])
    assert NOT_HIDDEN_SQL in retrieval.project_clause(project, None)[0]


def test_main_starts_uvicorn_and_warns_without_tokens(monkeypatch, caplog):
    from vera_mcp import __main__ as entry

    monkeypatch.delenv("MCP_TOKEN", raising=False)
    monkeypatch.delenv("MCP_TOKENS", raising=False)
    with patch.object(entry.uvicorn, "run") as run, caplog.at_level("ERROR", "vera_mcp"):
        entry.main()
    run.assert_called_once()
    assert run.call_args.kwargs["port"] == 8000
    assert "MCP_TOKEN" in caplog.text


def test_main_logs_configured_client_names(monkeypatch, caplog):
    from vera_mcp import __main__ as entry

    monkeypatch.setenv("MCP_TOKENS", "claude:" + "a" * 32 + ",codex:" + "b" * 32)
    with patch.object(entry.uvicorn, "run"), caplog.at_level("INFO", "vera_mcp"):
        entry.main()
    assert "claude, codex" in caplog.text


@pytest.mark.asyncio
async def test_search_client_maps_errors(monkeypatch):
    import httpx
    from vera_shared.search_client import SearchUnavailable, search_brain

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-internal-secret"] == "s"
        if request.url.path != "/search":
            raise AssertionError(request.url)
        return httpx.Response(503, text="overloaded")

    real = httpx.AsyncClient
    monkeypatch.setattr(
        "vera_shared.search_client.httpx.AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    with pytest.raises(SearchUnavailable) as exc:
        await search_brain("http://x", "s", "q")
    assert exc.value.status == 503 and "overloaded" in exc.value.detail
