import pytest

from app.self_extend import discovery
from app.self_extend.discovery import _looks_like_mcp, _score


def test_looks_like_mcp_filters_clients():
    assert _looks_like_mcp({"name": "some-mcp-server", "description": "MCP server"})
    assert not _looks_like_mcp({"name": "mcp-client", "description": "client"})
    assert not _looks_like_mcp({"name": "react", "description": "ui"})
    assert _looks_like_mcp({"name": "@org/foo-mcp", "description": "An MCP for X"})


def test_score_ranks_by_overlap():
    tokens = {"instagram", "dm"}
    high = _score({"name": "instagram-dm-mcp",
                   "description": "Send Instagram DMs", "score": {"final": 0.5}}, tokens)
    low = _score({"name": "random-mcp",
                  "description": "Generic tool", "score": 0.1}, tokens)
    assert high > low


@pytest.mark.asyncio
async def test_discover_handles_no_npm(monkeypatch):
    async def fake_npm_search(*args, **kwargs):
        return []
    monkeypatch.setattr(discovery, "_npm_search", fake_npm_search)
    out = await discovery.discover("nonsense xyz", top_n=2)
    assert isinstance(out, list)


@pytest.mark.asyncio
async def test_discover_includes_uvx_catalog(monkeypatch):
    async def fake_npm_search(*args, **kwargs):
        return []
    monkeypatch.setattr(discovery, "_npm_search", fake_npm_search)
    out = await discovery.discover("read web page fetch", top_n=3)
    assert any(c["name"] == "mcp-server-fetch" for c in out)


@pytest.mark.asyncio
async def test_rate_limit_blocks_after_hour_quota():
    from app.self_extend import rate_limit
    rate_limit._HOUR_LIMIT = 1
    rate_limit._DAY_LIMIT = 5
    ok1, _ = await rate_limit.check_and_consume("install")
    assert ok1
    ok2, reason = await rate_limit.check_and_consume("install")
    assert not ok2
    assert "hour" in reason
