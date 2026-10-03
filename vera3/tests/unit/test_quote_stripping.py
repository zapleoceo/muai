"""Отрезание истории ответа в письмах: Gmail/Apple (en/ru), Outlook, `>`-цитаты."""
from __future__ import annotations

import pytest
from vera_shared.ingest.quotes import strip_quoted

CASES = [
    ("gmail_en", "Thanks, see you.\n\nOn Mon, Aug 11, 2026 at 10:00 AM John Doe <j@x.example> wrote:\n> old\n> text", "Thanks, see you."),
    ("gmail_en_wrapped", "Ok.\n\nOn Mon, Aug 11, 2026 at 10:00 AM John Doe <j@x.example>\nwrote:\n> hi", "Ok."),
    ("gmail_ru", "Хорошо, договорились.\n\n11 авг. 2026 г., 10:00 Иван Тестов <i@x.example> написал(а):\n> привет", "Хорошо, договорились."),
    ("apple_ru", "Принято\n\nВт, 11 авг 2026 в 10:00, Иван пишет:\n> привет", "Принято"),
    ("outlook_rule", "Yes.\n\n________________________________\nFrom: A <a@x.example>\nSent: Monday, August 11, 2026 10:00 AM\nTo: B\nSubject: hi\n\nold", "Yes."),
    ("outlook_ru", "Да.\n\nОт: Иван <i@x.example>\nОтправлено: 11 августа 2026 г. 10:00\nКому: Я\nТема: привет\n\nстарое", "Да."),
    ("original", "Reply\n\n-----Original Message-----\nFrom: A\nold", "Reply"),
    ("original_ru", "Ответ\n-----Исходное сообщение-----\nОт: А", "Ответ"),
    ("inline_quote", "> quoted\nanswer line\n> more", "answer line"),
    ("all_quoted", "> only\n> quote", ""),
]


@pytest.mark.parametrize("raw,expected", [(c[1], c[2]) for c in CASES], ids=[c[0] for c in CASES])
def test_quoted_history_is_removed(raw, expected):
    assert strip_quoted(raw) == expected


def test_plain_text_and_prose_with_wrote_are_untouched():
    assert strip_quoted("He wrote: a book") == "He wrote: a book"
    assert strip_quoted("From: here we go\nand on") == "From: here we go\nand on"
    assert strip_quoted(None) == ""
