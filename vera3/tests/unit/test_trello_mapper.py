"""Действие Trello → событие: авторство, отсев шума, разбор updateCard."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(__file__), "..", "..",
    "services", "ingestor-trello", "src"))

from ingestor_trello.describe import describe  # noqa: E402
from ingestor_trello.mapper import action_to_event, parse_date  # noqa: E402

ME = "me-member-id"


def _action(kind: str, data: dict, *, by_me: bool = True, action_id: str = "a1") -> dict:
    creator = ({"id": ME, "username": "dima", "fullName": "Dima Z"} if by_me
               else {"id": "other-id", "username": "kolya", "fullName": "Коля П"})
    return {
        "id": action_id,
        "type": kind,
        "date": "2026-08-24T10:11:12.000Z",
        "idMemberCreator": creator["id"],
        "memberCreator": creator,
        "data": data,
    }


def _event(action: dict) -> dict | None:
    return action_to_event(action, me_id=ME, me_username="dima", board_name="Доска")


def test_parse_date_returns_naive_utc():
    dt = parse_date("2026-08-24T10:11:12.000Z")
    assert dt.tzinfo is None
    assert (dt.year, dt.month, dt.day, dt.hour) == (2026, 8, 24, 10)


def test_own_action_is_self_authored():
    ev = _event(_action("createCard", {
        "card": {"id": "c1", "name": "Сделать отчёт", "shortLink": "abc"},
        "list": {"name": "To Do"},
        "board": {"id": "b1", "name": "Доска"},
    }))
    assert ev is not None
    assert ev["content_text"].startswith("Author: Я [self]")
    assert ev["metadata_"]["author_role"] == "self"
    assert ev["metadata_"]["card_url"] == "https://trello.com/c/abc"
    assert ev["entity_hints"] == []


def test_foreign_action_carries_person_hint():
    ev = _event(_action("commentCard", {
        "text": "готово, посмотри",
        "card": {"id": "c1", "name": "Отчёт"},
        "board": {"id": "b1", "name": "Доска"},
    }, by_me=False))
    assert ev is not None
    assert ev["metadata_"]["author_role"] == "counterparty"
    assert ev["metadata_"]["author_username"] == "kolya"
    assert ev["entity_hints"] == [
        {"type": "person", "identifier": "kolya", "name": "Коля П"}
    ]
    assert ev["category"] == "comment"
    assert "готово, посмотри" in ev["content_text"]


def test_source_event_id_is_action_id():
    ev = _event(_action("createCard", {
        "card": {"name": "X"}, "board": {"name": "Доска"},
    }, action_id="act-42"))
    assert ev["source_event_id"] == "act-42"
    assert ev["source"] == "trello"


def test_card_move_reads_both_lists():
    text = describe(_action("updateCard", {
        "card": {"name": "Отчёт"},
        "old": {"idList": "l1"},
        "listBefore": {"name": "В работе"},
        "listAfter": {"name": "Готово"},
    }))
    assert text is not None and "В работе" in text and "Готово" in text


def test_due_set_and_cleared():
    set_text = describe(_action("updateCard", {
        "card": {"name": "Отчёт", "due": "2026-08-30T12:00:00.000Z"},
        "old": {"due": None},
    }))
    assert set_text is not None and "2026-08-30 12:00" in set_text
    cleared = describe(_action("updateCard", {
        "card": {"name": "Отчёт", "due": None}, "old": {"due": "2026-08-30T12:00:00.000Z"},
    }))
    assert cleared is not None and "снял срок" in cleared


def test_position_change_is_not_an_event():
    assert describe(_action("updateCard", {
        "card": {"name": "Отчёт"}, "old": {"pos": 100},
    })) is None
    assert _event(_action("updateCard", {
        "card": {"name": "Отчёт"}, "old": {"pos": 100},
    })) is None


def test_unknown_action_type_is_skipped():
    assert _event(_action("addAttachmentToCard", {"card": {"name": "X"}})) is None
    assert _event(_action("updateBoard", {"board": {"name": "Доска"}})) is None


def test_empty_comment_is_skipped():
    assert _event(_action("commentCard", {"text": "   ", "card": {"name": "X"}})) is None
