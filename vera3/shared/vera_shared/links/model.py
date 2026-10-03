"""Типы связи «событие ↔ сущность» — данные без логики (миграция 042)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

AUTHOR, RECIPIENT, PARTICIPANT, MENTIONED = "author", "recipient", "participant", "mentioned"
ROLES = (AUTHOR, RECIPIENT, PARTICIPANT, MENTIONED)
#: Роли «был в разговоре»: по ним фильтр участников и счёт совместных событий.
PRESENCE_ROLES = (AUTHOR, RECIPIENT, PARTICIPANT)

ALIAS, NICKNAME, VOICEPRINT, NAME_MATCH, MANUAL = (
    "alias", "nickname", "voiceprint", "name_match", "manual")
SOURCES_OF_LINK = (ALIAS, NICKNAME, VOICEPRINT, NAME_MATCH, MANUAL)

#: Виды событий для фильтров: звонок, переписка (чаты/DM), письмо.
KIND_SOURCES = {"call": ("voice",), "email": ("gmail",),
                "message": ("telegram", "slack", "instagram")}


@dataclass(frozen=True)
class Link:
    """Одна связь: кто, в какой роли, откуда известно. `token` — что сработало
    (прозвище, ярлык говорящего), у связей по алиасу пуст; `span` — где в событии."""
    event_id: int
    entity_id: int
    role: str
    source: str
    confidence: float = 1.0
    token: str = ""
    span: dict[str, Any] | None = None
    scope_ok: bool = True
