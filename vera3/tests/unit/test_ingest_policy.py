"""vera_shared.ingest_policy — денай-лист отправителей (в мозг не пишем)."""
from __future__ import annotations

import pytest
from vera_shared.ingest_policy import is_ignored_sender


@pytest.mark.parametrize("username", [
    "VerandamyBot", "verandamybot", "@VerandamyBot", "  @verandamybot  ",
])
def test_ignored_bot_detected_any_form(username):
    assert is_ignored_sender(username) is True


@pytest.mark.parametrize("username", [
    "zapleosoft", "itSTEPan_bot", "Dimondra_Ai_Bot",   # свои боты — пишем
    "verandamy", "verandamybot2", "myverandamybot",    # похожие, но другие
    None, "", "   ",
])
def test_others_are_ingested(username):
    assert is_ignored_sender(username) is False
