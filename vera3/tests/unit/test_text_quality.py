"""is_contentless: заглушки мессенджеров — без вектора, осознанные записи — с."""
from __future__ import annotations

import pytest
from vera_shared.text_quality import is_contentless

HEAD = ("Author: @u [counterparty]\nFrom: U (@u)\nChat: C (channel)\n"
        "Date: 2026-07-22T18:54:31+00:00\nDirection: received\n---\n")


@pytest.mark.parametrize("source", ["telegram", "slack", "instagram"])
@pytest.mark.parametrize("body", [
    "[photo]", "[video]", "[voice]", "[sticker]", "[document]", "[video_note]",
    "", "   ", "Ок", "Да", "Привет", "👍", "[photo] ок", "Спасибо!", "0123456789",
])
def test_chat_placeholders_and_short_replies_skipped(source, body):
    assert is_contentless(HEAD + body, source)


@pytest.mark.parametrize("body", [
    "Давайте встретимся завтра в офисе", "01234567890",
    "[photo]\nОписание: чек из кассы на 120 000", "Из кассы забрал деньги",
])
def test_chat_meaningful_bodies_kept(body):
    assert not is_contentless(HEAD + body, "telegram")


@pytest.mark.parametrize("source", ["claude", "vera_memory", "voice", "gmail",
                                   "claude_chat", "monitor"])
def test_deliberate_sources_never_skipped(source):
    assert not is_contentless("Купил хлеб", source)
    assert not is_contentless(HEAD + "Ок", source)


def test_telegram_placeholder_skipped_but_claude_memory_kept():
    assert is_contentless(HEAD + "[photo]", "telegram")
    assert not is_contentless("Купил хлеб", "claude")


def test_no_header_layout_is_not_judged_even_for_chat_source():
    assert not is_contentless("[photo]", "telegram")
    assert not is_contentless("Купил хлеб", "telegram")


def test_markdown_rule_inside_body_is_not_the_separator():
    # заголовка нет, «---» внутри markdown-тела: раскладка чужая — не трогаем
    assert not is_contentless("# Заметка\n\n---\n\nok", "telegram")


def test_only_first_separator_splits_header_from_body():
    body = "Список дел на неделю\n---\nкупить хлеб"
    assert not is_contentless(HEAD + body, "telegram")


def test_long_header_does_not_make_empty_body_meaningful():
    assert is_contentless(HEAD.replace("Chat: C", "Chat: " + "x" * 200) + "[photo]",
                          "telegram")


@pytest.mark.parametrize("empty", [None, "", "  \n "])
def test_empty(empty):
    assert is_contentless(empty, "claude")
