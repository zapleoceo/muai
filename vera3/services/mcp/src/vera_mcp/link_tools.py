"""MCP-инструменты связей событий с людьми: участники события, совместные события,
фильтр по людям для поиска и выборок.

Фильтр один для всех (`EventFilter`): ids через AND — «где были И A, И B»; `kind` —
call | message | email; `with_owner` — где был владелец. Связи строит индекс
`event_entities`; строки ниже `min_confidence` (угаданные по искажённому имени) и
прозвища вне области в выдачу не попадают.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field
from vera_shared.links import read as links_read
from vera_shared.links.filters import EventFilter, FilterError, build_where
from vera_shared.links.read import MAX_EVENTS
from vera_shared.timeutil import parse_iso_naive

IdList = Annotated[list[int] | None, Field(max_length=10)]
Kind = Literal["call", "message", "email"]
Role = Literal["author", "recipient", "participant", "mentioned"]


def link_filter(
    participant_ids: list[int] | None = None, mentioned_ids: list[int] | None = None,
    author_ids: list[int] | None = None, with_owner: bool = False,
    source: str | None = None, kind: str | None = None,
    start: str | None = None, end: str | None = None, min_confidence: float = 0.0,
) -> EventFilter:
    """Фильтр из аргументов инструмента; неверный вид или список — ValueError."""
    flt = EventFilter(
        tuple(participant_ids or ()), tuple(mentioned_ids or ()), tuple(author_ids or ()),
        with_owner, source, kind, parse_iso_naive(start) if start else None,
        parse_iso_naive(end) if end else None, min_confidence)
    try:
        build_where(flt, owner_id=0)        # проверка вида и размеров списков до обращения к базе
    except FilterError as e:
        raise ValueError(str(e)) from e
    return flt


async def event_participants(event_id: int) -> dict[str, Any]:
    """Кто был в событии и как: автор, получатели, участники созвона (по голосу или имени), упомянутые; у каждой связи источник (alias / voiceprint / name_match / nickname / manual), уверенность и ярлык. unresolved_speakers — голоса созвона без имени: назвать через voice_speaker_set. Participants, recipients, mentioned people and unresolved call speakers of one event."""
    found = await links_read.event_participants(event_id)
    if found is None:
        raise LookupError(f"event {event_id} not found")
    return found


async def co_occurrence(
    entity_a: int, entity_b: int, start: str | None = None, end: str | None = None,
    limit: Annotated[int, Field(ge=1, le=MAX_EVENTS)] = 50,
) -> dict[str, Any]:
    """События, где были ОБА человека (автор / получатель / участник созвона), за период: счёт по видам (звонки, переписка, письма) и список новых первыми. Events where both entities took part, by kind."""
    return await links_read.co_occurrence(
        entity_a, entity_b, parse_iso_naive(start) if start else None,
        parse_iso_naive(end) if end else None, limit)


LINK_TOOLS = (event_participants, co_occurrence)
