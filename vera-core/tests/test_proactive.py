"""Smoke tests for P1 pattern_miner + P2 proactive."""
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def test_stem_normalises_numbers_and_greetings():
    from app.brain.pattern_miner import _stem
    s1 = _stem("Привет, по должнику 12 от 25.05 нужно проверить статус")
    s2 = _stem("Hi, по должнику 7 от 27.05 нужно проверить статус")
    # numbers/dates and greetings collapse → same stem
    assert s1 == s2


def test_stem_keeps_meaningful_content():
    from app.brain.pattern_miner import _stem
    s = _stem("по должнику нужно проверить статус")
    assert "должнику" in s
    assert "статус" in s


def test_signature_deterministic():
    from app.brain.pattern_miner import _signature
    assert _signature(["a", "b"]) == _signature(["a", "b"])
    assert _signature(["a", "b"]) != _signature(["b", "a"])


@pytest.mark.asyncio
async def test_proactive_skips_outgoing_events():
    """Sent events must NOT trigger proactive DM (would loop on ourselves)."""
    from app.brain.proactive import maybe_propose
    fake_ev = MagicMock()
    fake_ev.id = 1
    fake_ev.entity_hints = []
    fake_ev.metadata_ = {"direction": "sent"}
    fake_ev.content_text = "test"
    fake_ev.account = "x"

    async def mock_session_call(*a, **kw):
        m = MagicMock()
        m.scalar_one_or_none = MagicMock(return_value=fake_ev)
        return m

    sess = AsyncMock()
    sess.execute = AsyncMock(side_effect=mock_session_call)
    sess.__aenter__ = AsyncMock(return_value=sess)
    sess.__aexit__ = AsyncMock(return_value=None)
    with patch("vera_shared.db.engine.get_session", return_value=sess), \
         patch("app.brain.proactive._send_proactive_dm") as send_mock:
        await maybe_propose(1)
    send_mock.assert_not_called()


def test_proactive_callback_router_registered():
    """Router must be wired in main; smoke test the import path."""
    from app.bot.proactive_callbacks import router
    # Router is a real aiogram Router with at least 1 callback handler
    assert router is not None
    assert len(router.callback_query.handlers) >= 1
