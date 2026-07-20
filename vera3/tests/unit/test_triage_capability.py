"""resolve_triage_capability() — chat:fast preferred, chat:smart fallback."""
from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(__file__), "..", "..",
    "services", "brain-triage", "src",
))

from brain_triage.triage_calls import (  # noqa: E402
    TRIAGE_CAPABILITIES,
    resolve_triage_capability,
)


def _cooldowns(mapping: dict[str, float]):
    async def fake(cap: str) -> float:
        return mapping.get(cap, 0.0)
    return AsyncMock(side_effect=fake)


@pytest.mark.asyncio
async def test_prefers_fast_when_available():
    with patch("brain_triage.triage_calls.llm_cooldown_remaining_s",
               _cooldowns({})):
        assert await resolve_triage_capability() == "chat:fast"


@pytest.mark.asyncio
async def test_falls_back_to_smart_when_fast_capped():
    with patch("brain_triage.triage_calls.llm_cooldown_remaining_s",
               _cooldowns({"chat:fast": 1800.0})):
        assert await resolve_triage_capability() == "chat:smart"


@pytest.mark.asyncio
async def test_none_when_both_capped():
    with patch("brain_triage.triage_calls.llm_cooldown_remaining_s",
               _cooldowns({"chat:fast": 1800.0, "chat:smart": 900.0})):
        assert await resolve_triage_capability() is None


def test_fast_is_first_preference():
    assert TRIAGE_CAPABILITIES[0] == "chat:fast"
