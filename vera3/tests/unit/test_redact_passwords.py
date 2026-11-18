"""Synthetic regressions for conversational password declarations."""
from __future__ import annotations

import pytest
from vera_shared.redact_view import MASK, redact_tree, redact_view


@pytest.mark.parametrize("text", [
    "CANARYp9! вот такой пароль введи",
    "Author: Я [self]\nWhere: ЛС\n---\nCANARYp9! вот такой пароль введи",
    '"CANARY phrase with spaces" вот такой пароль введи',
    "CANARYp9! это пароль",
    "CANARYp9! is the password",
    "пароль: CANARYp9!&tail;more",
    "password = CANARYp9!&tail;more",
    'пароль: "CANARY phrase with spaces"',
    "пароль CANARYp9!",
    "password is CANARYp9!",
    "пароль — CANARYp9!",
    'пароль "CANARY phrase with spaces"',
])
def test_conversational_password_is_fully_hidden(text: str) -> None:
    out = redact_view(text)
    assert "CANARY" not in out
    assert "tail" not in out and "more" not in out
    assert MASK in out
    assert redact_view(out) == out


@pytest.mark.parametrize("text", [
    "пароль не нужен", "password required", "пароль забыл",
    "token limit: 4096", "password: null", "password: false",
    "мы обсуждали пароль вчера", "https://x.test/path?id=1234",
])
def test_non_secret_prose_is_preserved(text: str) -> None:
    assert redact_view(text) == text


def test_read_surfaces_share_non_mutating_redaction() -> None:
    source = "CANARYp9! вот такой пароль введи"
    obj = {"answer": source, "events": [{"content_text": source}],
           "results": [{"content_preview": source}], "id": 123}
    clean = redact_tree(obj)
    assert "CANARY" not in str(clean)
    assert clean["id"] == 123
    assert obj["answer"] == source
