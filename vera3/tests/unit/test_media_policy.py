"""vera_shared.media_policy.should_recognize_media — recognition gating."""
from __future__ import annotations

import pytest
from vera_shared.media_policy import is_noise_chat, should_recognize_media


@pytest.mark.parametrize("media_kind,chat_kind,expected", [
    # voice/audio → always (whisper, valuable everywhere)
    ("voice", "channel", True),
    ("voice", "private", True),
    ("audio", "group", True),
    # photos: yes in private/group, NO in broadcast channels
    ("photo", "private", True),
    ("photo", "group", True),
    ("photo", "channel", False),
    ("image", "channel", False),
    ("image", "group", True),
    # stickers: never (Dima 2026-07-20 — skip entirely)
    ("sticker", "private", False),
    ("sticker", "group", False),
    ("sticker", "channel", False),
    # non-recognizable kinds
    ("video", "private", False),
    ("video_note", "private", False),
    ("document", "private", False),
    (None, "private", False),
    # unknown chat_kind: photo still recognized (only 'channel' is excluded)
    ("photo", None, True),
])
def test_should_recognize_media(media_kind, chat_kind, expected):
    assert should_recognize_media(media_kind, chat_kind) is expected


# ─── шумные паблики (Дима 2026-07-29: «это все в топку») ────────────────────


@pytest.mark.parametrize("title", [
    "NEXTA Live",
    "NEXTA Live Chat",                                   # чат-обсуждение канала
    "Українці у Вʼєтнамі 🇺🇦🇻🇳 … Ukrainians in Vietnam",
    "Українці на Шрі-Ланці 🇺🇦❤️🇱🇰",
    "Українці у Нячанзі 🇺🇦🇻🇳",
    "Українці на Шрі - курилка",
    "ХДніпро 🇺🇦",
    "ВЕЛИГАМНОСТЬ 🏄‍♀️🛵🏖️ ШРИ-ЛАНКА",
    "Квизда Нячанг",
    "ИИ - БОТЫ | НЕЙРОСЕТИ",
    "ChatGPT | Штучний нейрон",
    "Канал Лучкова",
    "[BadComedian]",
    "  nexta live  ",                                    # регистр/пробелы
])
def test_noise_chats_detected(title):
    assert is_noise_chat(title) is True


@pytest.mark.parametrize("title", [
    "Veranda менеджмент", "Веранда сотрудники", "Старшие и отчеты",
    "Jakarta sales", "GameZone & Veranda", "ITS | Tech4You",
    "Быть Или",                    # не классифицирован Димой — НЕ режем
    "Олег Демченко", "Султан", "Евочка Моя",
    None, "",
])
def test_work_and_personal_chats_are_not_noise(title):
    assert is_noise_chat(title) is False


@pytest.mark.parametrize("kind", ["photo", "image"])
def test_photos_from_noise_groups_skipped(kind):
    # группа, не канал — старое правило её не ловило
    assert should_recognize_media(kind, "group", "ВЕЛИГАМНОСТЬ 🏄‍♀️ ШРИ-ЛАНКА") is False
    assert should_recognize_media(kind, "group", "Veranda менеджмент") is True


def test_voice_from_noise_chat_still_recognized():
    # whisper — отдельный дешёвый пул, речь ценна везде
    assert should_recognize_media("voice", "group", "NEXTA Live Chat") is True


def test_default_chat_title_keeps_old_behaviour():
    # вызов без chat_title (легаси-путь) не должен ничего резать
    assert should_recognize_media("photo", "group") is True


# ─── should_extract_relations: граф связей не строим по рекламе ─────────────


def test_channel_posts_never_feed_the_graph():
    # инцидент 2026-08-06: из рекламы SUP-тура в канале родилось
    # `Дима -[client_of]-> T2T`, хотя имени в тексте не было вовсе
    from vera_shared.media_policy import should_extract_relations
    assert should_extract_relations({"chat_kind": "channel"}) is False
    assert should_extract_relations(
        {"chat_kind": "channel", "chat_title": "T2T | Афиша Нячанга"}) is False


def test_noise_groups_never_feed_the_graph():
    from vera_shared.media_policy import should_extract_relations
    assert should_extract_relations(
        {"chat_kind": "group", "chat_title": "ВЕЛИГАМНОСТЬ 🏄 ШРИ-ЛАНКА"}) is False
    assert should_extract_relations(
        {"chat_kind": "group", "chat_title": "NEXTA Live Chat"}) is False


@pytest.mark.parametrize("meta", [
    {"chat_kind": "group", "chat_title": "Veranda менеджмент"},
    {"chat_kind": "private", "chat_title": "Олег Демченко"},
    {"chat_kind": "supergroup", "chat_title": "Jakarta sales"},
    {},          # метаданных нет — не наказываем, строим
    None,
])
def test_real_conversations_still_feed_the_graph(meta):
    from vera_shared.media_policy import should_extract_relations
    assert should_extract_relations(meta) is True
