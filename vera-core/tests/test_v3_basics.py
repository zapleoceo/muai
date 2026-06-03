"""Smoke tests for v3 brain + decide modules. Run with pytest.

These verify the contracts compile, signatures hash deterministically,
scoring math respects bands + NoGo + AUTO_SAFE cap. Graph-touching
code paths are exercised only as far as building the call shape;
actual Neo4j is mocked or skipped.
"""
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.brain import patterns as P
from app.decide.scoring import AUTO_SAFE_TOOLS, Candidate, score
from app.orchestrator.tool_router import tool_reversibility as _reversibility
from app.decide.dispatch import _band, decide
from app.sources.base import Attachment, EntityHint, EventEnvelope


def test_envelope_round_trip():
    env = EventEnvelope(
        source="gmail", source_event_id="x:1",
        occurred_at=datetime.utcnow(), content_text="hi",
        entity_hints=[EntityHint(type="person", identifier="a@b.c")],
    )
    assert env.source == "gmail"
    assert env.entity_hints[0].identifier == "a@b.c"
    assert isinstance(env.attachments, list)


def test_signature_deterministic_and_isolates_order():
    h1 = [{"type": "person", "identifier": "a@b.c"},
          {"type": "chat", "identifier": "42"}]
    h2 = [{"type": "chat", "identifier": "42"},
          {"type": "person", "identifier": "a@b.c"}]
    assert P.signature_for(h1, "Archive") == P.signature_for(h2, "Archive")
    assert P.signature_for(h1, "Archive") != P.signature_for(h1, "Reply")


def test_signature_ignores_volatile_hint_types():
    base = [{"type": "person", "identifier": "a@b.c"}]
    s1 = P.signature_for(base, "X")
    s2 = P.signature_for(base + [{"type": "thread", "identifier": "noise"}], "X")
    assert s1 == s2  # 'thread' type is not in the stable set


def test_band_thresholds():
    # Thresholds tuned 2026-06-01 after user complained score-3.8 cards
    # flooded the group. Old: 3.0/7.0. New: 5.0/8.0. Ask renamed to silent.
    assert _band(8.5) == "auto"
    assert _band(8.0) == "auto"
    assert _band(7.99) == "propose"
    assert _band(5.0) == "propose"
    assert _band(4.99) == "silent"
    assert _band(0.0) == "silent"


def test_reversibility_heuristic():
    assert _reversibility(None) == 0.5
    assert _reversibility("gmail_modify_thread") == 0.9
    assert _reversibility("gmail_send_reply") == 0.1
    assert _reversibility("delete_thread") == 0.0
    assert _reversibility("random_tool") == 0.5


def test_auto_safe_tools_membership():
    # Tools that the v2 send/reply path uses must NOT be auto-safe.
    assert "gmail_modify_thread" in AUTO_SAFE_TOOLS
    assert "gmail_send_reply" not in AUTO_SAFE_TOOLS
    assert "telegram_send_message" not in AUTO_SAFE_TOOLS


@pytest.mark.asyncio
async def test_scoring_nogo_blocks_to_zero():
    cand = Candidate(label="X", tool="dangerous_send", args={})
    fake_nogo = [{"id": "no-1"}]
    with patch("app.decide.scoring._nogo_violations",
                new=AsyncMock(return_value=["no-1"])), \
         patch("app.brain.patterns.get_pattern",
                new=AsyncMock(return_value=None)), \
         patch("app.decide.scoring._value_alignment",
                new=AsyncMock(return_value=0.9)), \
         patch("app.decide.scoring._goal_contribution",
                new=AsyncMock(return_value=0.9)):
        result = await score(cand, [{"type": "person", "identifier": "a@b"}])
    assert result.score == 0.0
    assert result.blocked_by == "no-1"


@pytest.mark.asyncio
async def test_scoring_caps_unsafe_tool_below_auto():
    # Even with perfect components, a send-tool can't reach 7.0
    cand = Candidate(label="Reply", tool="gmail_send_reply", args={})
    fake_pattern = {"observation_count": 100, "confirmation_count": 100,
                     "correction_count": 0, "weight": 100.0}
    with patch("app.decide.scoring._nogo_violations",
                new=AsyncMock(return_value=[])), \
         patch("app.brain.patterns.get_pattern",
                new=AsyncMock(return_value=fake_pattern)), \
         patch("app.decide.scoring._value_alignment",
                new=AsyncMock(return_value=1.0)), \
         patch("app.decide.scoring._goal_contribution",
                new=AsyncMock(return_value=1.0)):
        result = await score(cand, [{"type": "person", "identifier": "a@b"}])
    assert result.score < 7.0
    assert result.score == 6.9  # explicit cap value


@pytest.mark.asyncio
async def test_scoring_safe_tool_with_history_can_auto():
    cand = Candidate(label="Archive", tool="gmail_modify_thread", args={})
    fake_pattern = {"observation_count": 20, "confirmation_count": 10,
                     "correction_count": 0, "weight": 10.0}
    with patch("app.decide.scoring._nogo_violations",
                new=AsyncMock(return_value=[])), \
         patch("app.brain.patterns.get_pattern",
                new=AsyncMock(return_value=fake_pattern)), \
         patch("app.decide.scoring._value_alignment",
                new=AsyncMock(return_value=0.8)), \
         patch("app.decide.scoring._goal_contribution",
                new=AsyncMock(return_value=0.7)):
        result = await score(cand, [{"type": "person", "identifier": "a@b"}])
    assert result.score >= 7.0


@pytest.mark.asyncio
async def test_decide_on_empty_graph_returns_ask():
    with patch("app.decide.dispatch._enumerate_candidates",
                new=AsyncMock(return_value=[])):
        d = await decide([{"type": "person", "identifier": "x"}])
    assert d.band == "ask"
    assert d.chosen is not None
    assert d.chosen.candidate.label == "спросить Диму"
