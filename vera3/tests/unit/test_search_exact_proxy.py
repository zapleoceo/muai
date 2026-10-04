"""The in-repository MCP and gateway wrappers preserve explicit ID queries."""
from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import AsyncMock

import httpx
import pytest
from brain_search import app as search_app
from brain_search.models import AnswerResponse, SearchQuery
from gateway.query import SearchProxyRequest, search_proxy
from vera_mcp import read_tools
from vera_shared.db.models import EventRow


@pytest.mark.parametrize("wrapper", ["mcp", "gateway"])
@pytest.mark.asyncio
async def test_explicit_id_survives_proxy_to_base_event(sqlite_db, monkeypatch, wrapper):
    monkeypatch.setenv("INTERNAL_SECRET", "test-internal-secret")
    async with sqlite_db() as session:
        session.add(EventRow(id=900101, source="gmail", source_event_id="synthetic",
                             occurred_at=datetime(2026, 10, 4, 12), content_text="Notice"))

    async def synthesize(query, rows, *_args, **_kwargs):
        return AnswerResponse(answer="Synthetic evidence", provider="test", cost_usd=0,
                              results=[{"event_id": row.id, "source": row.source,
                                        "occurred_at": str(row.occurred_at),
                                        "content_preview": row.content_text,
                                        "importance": row.importance, "score": 1.0}
                                       for row in rows])

    monkeypatch.setattr(search_app, "synthesize", synthesize)
    embed = AsyncMock(side_effect=AssertionError("exact lookup must skip embedding"))
    report = AsyncMock(side_effect=AssertionError("exact lookup must skip reports"))
    monkeypatch.setattr(search_app, "embed_query", embed)
    monkeypatch.setattr(search_app, "_try_report", report)
    question = "Check [event:900101]"

    async def handler(request):
        body = json.loads(request.content)
        assert request.url.path == "/search"
        assert body["q"] == question and body["limit"] == 1
        answer = await search_app.search(SearchQuery(**body), "test-internal-secret")
        return httpx.Response(200, json=answer.model_dump())

    real_client = httpx.AsyncClient
    monkeypatch.setattr("vera_shared.search_client.httpx.AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    if wrapper == "mcp":
        result = await read_tools.search(question, limit=1)
    else:
        result = await search_proxy(SearchProxyRequest(q=question, limit=1),
                                    "test-internal-secret")
    assert [row["event_id"] for row in result["results"]] == [900101]
    embed.assert_not_awaited()
    report.assert_not_awaited()
