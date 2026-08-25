"""Суточный дайджест сроков: порядок, просрочка, пустой случай."""
from __future__ import annotations

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(
    os.path.dirname(__file__), "..", "..",
    "services", "ingestor-trello", "src"))

from ingestor_trello.digest import build_digest, digest_event  # noqa: E402

NOW = datetime(2026, 8, 25, 9, 0, 0)


def _card(name: str, due: str | None, done: bool = False) -> dict:
    return {"name": name, "due": due, "dueComplete": done}


def test_cards_without_due_are_ignored():
    assert build_digest([("Доска", [_card("Без срока", None)])], NOW) is None


def test_completed_due_is_ignored():
    assert build_digest(
        [("Доска", [_card("Сдал", "2026-08-20T10:00:00.000Z", done=True)])], NOW,
    ) is None


def test_overdue_counted_and_marked():
    built = build_digest([("Доска", [
        _card("Просроченная", "2026-08-20T10:00:00.000Z"),
        _card("Будущая", "2026-08-30T10:00:00.000Z"),
    ])], NOW)
    assert built is not None
    text, total, overdue = built
    assert (total, overdue) == (2, 1)
    assert "ПРОСРОЧЕНО" in text
    # Ближайший срок идёт первым.
    assert text.index("Просроченная") < text.index("Будущая")


def test_digest_event_is_idempotent_per_day():
    built = build_digest([("Доска", [_card("X", "2026-08-30T10:00:00.000Z")])], NOW)
    assert built is not None
    ev = digest_event(built[0], built[1], built[2], NOW, "dima")
    assert ev["source_event_id"] == "digest:2026-08-25"
    assert ev["metadata_"]["author_role"] == "self"
    assert ev["category"] == "digest"
