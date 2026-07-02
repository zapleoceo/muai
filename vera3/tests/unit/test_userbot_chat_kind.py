"""classify_chat_kind — приватный/группа/канал, включая регресс на баг
где супергруппы (Telethon Channel-объект) ошибочно считались каналами."""
from __future__ import annotations

import os

os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "x")
os.environ.setdefault("TELEGRAM_PHONE", "+10000000000")

from ingestor_telegram.userbot import classify_chat_kind  # noqa: E402


def test_private_dm():
    assert classify_chat_kind("user", False) == "private"


def test_legacy_small_group():
    assert classify_chat_kind("chat", False) == "group"
    assert classify_chat_kind("chatfull", False) == "group"


def test_supergroup_is_group_not_channel():
    """Регресс: Telethon отдаёт супергруппы как Channel-класс — без
    megagroup=True флага их бы ошибочно посчитали broadcast-каналом."""
    assert classify_chat_kind("channel", True) == "group"


def test_broadcast_channel():
    assert classify_chat_kind("channel", False) == "channel"


def test_unknown_type_is_other():
    assert classify_chat_kind("bot", False) == "other"
    assert classify_chat_kind("unknown", True) == "other"
