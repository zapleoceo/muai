"""The brain-write helpers must be fire-and-forget and never raise."""
import asyncio
from datetime import datetime
from unittest.mock import patch, AsyncMock

import pytest

from app.graph import write as gw


@pytest.mark.asyncio
async def test_write_decision_does_not_raise():
    gw.write_decision(1, "telegram", "@user", "архивировать",
                      "gmail_modify_thread", "test summary")
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_write_rejection_does_not_raise():
    gw.write_rejection(2, "telegram", "@spammer", "ad message")
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_write_instruction_does_not_raise():
    gw.write_instruction(169510539, "игнорируй verandamybot")
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_add_swallows_failures():
    with patch("app.graph.write.get_graphiti", new=AsyncMock(side_effect=RuntimeError("no neo4j"))):
        await gw._add("test/x", "body")  # must not raise
