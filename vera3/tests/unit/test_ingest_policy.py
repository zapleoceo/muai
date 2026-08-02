"""vera_shared.ingest_policy — денай-лист отправителей (в мозг не пишем)."""
from __future__ import annotations

import pytest
from vera_shared.ingest_policy import is_ignored_chat, is_ignored_sender


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


# ─── игнор чата целиком (обе стороны переписки) ─────────────────────────────


@pytest.mark.parametrize("chat_username,chat_title", [
    ("leomatchbot", None),
    ("@LeoMatchBot", None),
    (None, "Дайвінчик 🇺🇦 | Leo – знайомства, спілкування"),
    (None, "дайвинчик"),                       # рус. написание
    ("someone", "Дайвінчик 🇺🇦 | Leo"),         # ловится по названию
])
def test_ignored_chat_detected(chat_username, chat_title):
    assert is_ignored_chat(chat_username, chat_title) is True


@pytest.mark.parametrize("chat_username,chat_title", [
    (None, "Veranda менеджмент"),
    ("zapleosoft", "Старшие и отчеты"),
    (None, "Евочка Моя"),
    (None, None),
    ("", ""),
])
def test_normal_chats_ingested(chat_username, chat_title):
    assert is_ignored_chat(chat_username, chat_title) is False


def test_swipes_from_owner_are_dropped_too():
    """Регресс: фильтр по ОТПРАВИТЕЛЮ не ловит исходящие 👎 (sender=Дима),
    поэтому нужен игнор чата."""
    assert is_ignored_sender("zapleosoft") is False          # сам Дима — не бот
    assert is_ignored_chat("leomatchbot", "Дайвінчик") is True  # но чат режется
