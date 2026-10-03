"""Типизированная строка кандидата вместо r[6], r[7], r[8] по позициям.

Первые девять колонок в SQL стоят строго по порядку (retrieval._select и
ann.ann_rows_sql собирают их одинаково), остальные приходят по именам и
могут отсутствовать: у SQLite нет vec_sim, у старых тестовых строк нет
метаданных автора.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, NamedTuple

#: Колонки из events.metadata: признак бота-автора и авторство для агента.
#: Через lower(...) LIKE, а не ILIKE — запрос идёт и на SQLite в тестах.
META_COLUMNS = (
    "lower(metadata->>'sender_username') LIKE '%bot' AS is_bot, "
    "metadata->>'author_role' AS author_role, "
    "metadata->>'author_label' AS author_label, "
    "metadata->>'chat_title' AS chat_title"
)


class Candidate(NamedTuple):
    id: int
    source: str
    source_event_id: str
    occurred_at: datetime
    content_text: str | None
    importance: int | None
    embedding: Any
    rank: float | None
    account: str | None
    vec_sim: float | None = None
    is_bot: bool = False
    author_role: str | None = None
    author_label: str | None = None
    chat_title: str | None = None

    @classmethod
    def of(cls, row: Any) -> Candidate:
        if isinstance(row, cls):
            return row
        values = tuple(row)
        head = values[:9] + (None,) * max(0, 9 - len(values))
        return cls(
            *head,
            vec_sim=getattr(row, "vec_sim", None),
            is_bot=bool(getattr(row, "is_bot", False)),
            author_role=getattr(row, "author_role", None),
            author_label=getattr(row, "author_label", None),
            chat_title=getattr(row, "chat_title", None),
        )
