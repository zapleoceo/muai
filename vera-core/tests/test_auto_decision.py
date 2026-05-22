"""Auto-decision logic: confidence derived from replay count, gated by
preferences threshold + min_repeats, never fires for non-auto-safe tools."""
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.bot import preferences
from app.events.store import save_event
from app.triage import replay as rp
from app.triage.dispatcher import _pick_auto_action


def _proposal(actions, confidence=0.99):
    return SimpleNamespace(actions=actions, confidence=confidence,
                            summary="", reasoning="", urgency="low",
                            context_used=[])


@pytest.mark.asyncio
async def test_no_replay_means_no_auto():
    occurred = datetime.utcnow()
    ev, _ = await save_event(
        source="gmail", source_event_id="auto-1", account="a@b.c",
        category="x", content_text="x", content_extra=None,
        entity_hints=[{"type": "person", "identifier": "stranger@x.com"}],
        metadata=None, occurred_at=occurred,
    )
    p = _proposal([
        {"default": True, "tool": "gmail_modify_thread", "args": {}, "label": "X"},
    ], confidence=0.99)
    out = await _pick_auto_action(ev, p)
    assert out is None  # no replay history → no auto


@pytest.mark.asyncio
async def test_replay_below_min_repeats_no_auto():
    occurred = datetime.utcnow()
    ev, _ = await save_event(
        source="gmail", source_event_id="auto-2", account="a@b.c",
        category="x", content_text="x", content_extra=None,
        entity_hints=[{"type": "person", "identifier": "repeat-low@x.com"}],
        metadata=None, occurred_at=occurred,
    )
    await rp.record(ev, "Archive", "gmail_modify_thread", {"action": "archive"})
    await rp.record(ev, "Archive", "gmail_modify_thread", {"action": "archive"})
    p = _proposal([
        {"default": True, "tool": "gmail_modify_thread",
         "args": {"action": "archive"}, "label": "Archive", "replay": True},
    ], confidence=0.99)
    out = await _pick_auto_action(ev, p)
    assert out is None  # only 2 prior → < default min_repeats=3


@pytest.mark.asyncio
async def test_replay_threshold_met_fires_auto():
    occurred = datetime.utcnow()
    ev, _ = await save_event(
        source="gmail", source_event_id="auto-3", account="a@b.c",
        category="x", content_text="x", content_extra=None,
        entity_hints=[{"type": "person", "identifier": "trusted@x.com"}],
        metadata=None, occurred_at=occurred,
    )
    for _ in range(4):
        await rp.record(ev, "Archive", "gmail_modify_thread", {"action": "archive"})
    p = _proposal([
        {"default": True, "tool": "gmail_modify_thread",
         "args": {"action": "archive"}, "label": "Archive", "replay": True},
    ], confidence=0.97)
    out = await _pick_auto_action(ev, p)
    assert out is not None
    assert out["tool"] == "gmail_modify_thread"


@pytest.mark.asyncio
async def test_non_auto_safe_tool_blocked():
    occurred = datetime.utcnow()
    ev, _ = await save_event(
        source="gmail", source_event_id="auto-4", account="a@b.c",
        category="x", content_text="x", content_extra=None,
        entity_hints=[{"type": "person", "identifier": "send-target@x.com"}],
        metadata=None, occurred_at=occurred,
    )
    for _ in range(10):
        await rp.record(ev, "Reply", "gmail_send_reply", {"body": "ok"})
    p = _proposal([
        {"default": True, "tool": "gmail_send_reply",  # not in AUTO_SAFE
         "args": {"body": "ok"}, "label": "Reply", "replay": True},
    ], confidence=1.0)
    out = await _pick_auto_action(ev, p)
    assert out is None  # send-tools never auto-fire even with strong history


@pytest.mark.asyncio
async def test_preferences_threshold_blocks_below_value():
    await preferences.set("auto_threshold", 0.99)
    try:
        occurred = datetime.utcnow()
        ev, _ = await save_event(
            source="gmail", source_event_id="auto-5", account="a@b.c",
            category="x", content_text="x", content_extra=None,
            entity_hints=[{"type": "person", "identifier": "thr-test@x.com"}],
            metadata=None, occurred_at=occurred,
        )
        for _ in range(3):
            await rp.record(ev, "X", "gmail_modify_thread", {})
        p = _proposal([
            {"default": True, "tool": "gmail_modify_thread", "args": {},
             "label": "X", "replay": True},
        ], confidence=0.96)
        assert await _pick_auto_action(ev, p) is None
    finally:
        await preferences.set("auto_threshold", 0.95)


def test_default_prefs_define_thresholds():
    assert "auto_threshold" in preferences._DEFAULTS
    assert "auto_min_repeats" in preferences._DEFAULTS
