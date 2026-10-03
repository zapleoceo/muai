"""is_contentless: тела-заглушки и реплики в пару символов — без вектора."""
from __future__ import annotations

import pytest
from vera_shared.text_quality import is_contentless

HEAD = ("Author: @u [counterparty]\nFrom: U (@u)\nChat: C (channel)\n"
        "Date: 2026-07-22T18:54:31+00:00\nDirection: received\n---\n")


@pytest.mark.parametrize("body", [
    "[photo]", "[video]", "[voice]", "[sticker]", "[document]", "[video_note]",
    "", "   ", "Ок", "Да", "Привет", "👍", "[photo] ок", "Спасибо!", "0123456789",
])
def test_contentless_bodies(body):
    assert is_contentless(HEAD + body)


@pytest.mark.parametrize("body", [
    "Давайте встретимся завтра в офисе", "01234567890",
    "[photo]\nОписание: чек из кассы на 120 000", "Из кассы забрал деньги",
])
def test_meaningful_bodies(body):
    assert not is_contentless(HEAD + body)


def test_header_alone_does_not_count():
    # длинный заголовок не делает пустое тело содержательным
    assert is_contentless(HEAD.replace("Chat: C", "Chat: " + "x" * 200) + "[photo]")


def test_email_subject_counts():
    head = "Author: a\nFrom: a@b.c\nSubject: Счёт за октябрь по договору\n"
    assert not is_contentless(head + "---\nok")
    assert is_contentless("Author: a\nSubject: Re:\n---\nok")


def test_without_separator_judges_whole_text():
    assert is_contentless("[photo]")
    assert not is_contentless("deploy finished successfully")


@pytest.mark.parametrize("empty", [None, "", "  \n "])
def test_empty(empty):
    assert is_contentless(empty)
