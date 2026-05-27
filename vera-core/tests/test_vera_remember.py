"""vera_remember/recall: basic shape checks (graph-mocked)."""
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_remember_creates_memo_node():
    """remember() must MERGE a :Memo node, not :Pref or :Value."""
    from app.brain import identity as ID
    captured: dict = {}
    fake_client = AsyncMock()
    fake_session = AsyncMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=None)
    async def fake_run(query, **params):
        captured["query"] = query
        captured["params"] = params
        return AsyncMock()
    fake_session.run = fake_run
    fake_client.driver.session = lambda **kw: fake_session
    # identity.py imports get_graphiti inside the function — patch the source.
    with patch("app.graph.client.get_graphiti",
                new=AsyncMock(return_value=fake_client)), \
         patch("app.config.get_settings") as gs:
        gs.return_value.neo4j_database = "test"
        nid = await ID.remember("почта itstep.org = IT Step Indonesia",
                                  scope="email_routing")
    assert nid.startswith("memo_")
    assert ":Memo" in captured["query"]
    assert captured["params"]["statement"] == "почта itstep.org = IT Step Indonesia"
    assert captured["params"]["scope"] == "email_routing"


def test_remember_tool_registered():
    """The HTTP tool surface must expose vera_remember + vera_recall."""
    from app.system.tools import HANDLERS
    assert "vera_remember" in HANDLERS
    assert "vera_recall" in HANDLERS


def test_remember_spec_exists():
    from app.system.routes import TOOL_SPECS
    names = [s["name"] for s in TOOL_SPECS]
    assert "vera_remember" in names
    assert "vera_recall" in names
