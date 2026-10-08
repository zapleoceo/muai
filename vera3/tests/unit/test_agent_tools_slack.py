"""Synthetic contracts for Slack searches through the agent tool boundary."""
from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from brain_search import agent, agent_tools
from brain_search.rows import Candidate
from pydantic import ValidationError

SEARCH_TOOL = next(tool for tool in agent_tools.BUILTIN_SPECS if tool.name == "search_events")


def test_search_args_accept_slack() -> None:
    args = agent_tools.SearchEventsArgs(q="synthetic note", source="slack")

    assert args.source == "slack"


def test_search_args_reject_unknown_source() -> None:
    with pytest.raises(ValidationError):
        agent_tools.SearchEventsArgs(source="unsupported-source")


def test_search_source_schema_matches_validation() -> None:
    expected = {"telegram", "gmail", "instagram", "slack", "vera_chat", "any"}
    validation_enum = agent_tools.SearchEventsArgs.model_json_schema()["properties"]["source"]["enum"]
    tool_enum = SEARCH_TOOL.params_schema["properties"]["source"]["enum"]

    assert set(validation_enum) == expected
    assert set(tool_enum) == expected


def test_rendered_tool_advertises_slack() -> None:
    rendered = agent._render_tools([SEARCH_TOOL])
    description, encoded_schema = rendered.split("params_schema: ", 1)
    schema = json.loads(encoded_schema)

    assert "slack" in description
    assert "slack" in schema["properties"]["source"]["enum"]


@pytest.mark.asyncio
async def test_slack_search_preserves_filter_dates_and_result(monkeypatch: pytest.MonkeyPatch) -> None:
    candidate = Candidate(
        id=900001, source="slack", source_event_id="CSYNTHETIC:1234567890.000001",
        occurred_at=datetime(2026, 10, 1, 12), content_text="Synthetic Slack note.",
        importance=50, embedding=None, rank=1.0, account="synthetic",
        author_role="counterparty", author_label="Synthetic author",
        chat_title="Synthetic channel",
        source_permalink="https://synthetic.slack.com/archives/CSYNTHETIC/p1234567890000001",
    )
    search = AsyncMock(return_value=(None, [(1.0, candidate)]))
    monkeypatch.setattr(agent_tools, "search_ranked", search)

    result = await agent_tools.execute_tool(SEARCH_TOOL, {
        "q": "synthetic note", "source": "slack", "limit": 7,
        "date_from": "2026-10-01", "date_to": "2026-10-02",
    })

    search.assert_awaited_once_with(
        "synthetic note", limit=7, source="slack",
        time_range=(datetime(2026, 9, 30, 17), datetime(2026, 10, 2, 17)),
    )
    assert result == {"found": 1, "events": [{
        "event_id": 900001, "source": "slack", "source_url": candidate.source_permalink,
        "occurred_at": "2026-10-01 12:00:00 UTC", "author_role": "counterparty",
        "author_label": "Synthetic author", "chat_title": "Synthetic channel",
        "preview": "Synthetic Slack note.",
    }]}


@pytest.mark.asyncio
async def test_invalid_source_is_rejected_before_search(monkeypatch: pytest.MonkeyPatch) -> None:
    search = AsyncMock()
    monkeypatch.setattr(agent_tools, "search_ranked", search)

    result = await agent_tools.execute_tool(SEARCH_TOOL, {
        "q": "synthetic note", "source": "unsupported-source",
    })

    assert result["error"] == "invalid params"
    search.assert_not_awaited()


@pytest.mark.asyncio
async def test_default_source_still_searches_any(monkeypatch: pytest.MonkeyPatch) -> None:
    search = AsyncMock(return_value=(None, []))
    monkeypatch.setattr(agent_tools, "search_ranked", search)

    result = await agent_tools.execute_tool(SEARCH_TOOL, {"q": "synthetic note"})

    search.assert_awaited_once_with("synthetic note", limit=20, source="any", time_range=None)
    assert result == {"found": 0, "events": []}
