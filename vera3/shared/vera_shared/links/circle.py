"""Круг разговора: кто мог быть назван одиночным именем в этом событии.

Одиночное имя («Дима») указывает на человека только внутри круга: участники чата,
собеседник лички, автор, адресаты письма, сам владелец (события приходят из его
аккаунта). Второй круг — сильные контакты автора — берётся, лишь когда в первом
подходящего нет. Имя, подошедшее двоим (два Димы в чате), не указывает ни на кого.
Из этой же логики растут связи `mentioned` (matcher) и разрешение имён при извлечении
связей (`rel_extract`), и чистка прошлых ошибок (`namesake_plan`).
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import bindparam, select, text

from vera_shared.db.engine import get_session
from vera_shared.db.models import EventRow
from vera_shared.ingest.envelope import message_body
from vera_shared.links.context import ContextBuilder, EventFacts, EventView
from vera_shared.links.matcher import PersonNames
from vera_shared.links.names import NameResolver


@dataclass(frozen=True)
class EventCircle:
    primary: frozenset[int]
    extended: frozenset[int]
    text: str = ""


def circle_of(facts: EventFacts, owner: int | None) -> tuple[frozenset[int], frozenset[int]]:
    """(первый круг, второй круг). Владелец входит в круг только там, где есть разговор (чат,
    личка, адресаты письма): в записке самому себе или запросе к поисковику «Дима» — не он."""
    conversation = facts.ctx.chat_key is not None or bool(facts.recipients)
    primary = set(facts.ctx.participants) | set(facts.recipients)
    primary |= {facts.author} if facts.author is not None else set()
    if conversation and owner is not None:
        primary.add(owner)
    return frozenset(primary), facts.ctx.extended if conversation else frozenset()


async def event_circle(event_id: int) -> EventCircle | None:
    """Круг события; None — события нет (или оно скрыто): судить нечем."""
    async with get_session() as s:
        row = (await s.execute(select(EventRow).where(EventRow.id == event_id))).scalar_one_or_none()
    if row is None:
        return None
    body = message_body(row.content_text)
    view = EventView(row.id, row.source, body, row.metadata_ or {})
    builder = ContextBuilder()
    facts = (await builder.build([view]))[row.id]
    primary, extended = circle_of(facts, builder.owner)
    return EventCircle(primary, extended, body)


async def _persons(ids: frozenset[int]) -> list[PersonNames]:
    if not ids:
        return []
    async with get_session() as s:
        rows = (await s.execute(
            text("SELECT id, name FROM entities WHERE type = 'person' AND id IN :ids")
            .bindparams(bindparam("ids", expanding=True)), {"ids": sorted(ids)})).all()
    return [PersonNames(r[0], r[1]) for r in rows if r[1]]


async def resolve_short_name(name: str, circle: EventCircle) -> int | None:
    """Единственный человек первого круга с такой формой имени, иначе (если в первом
    никого) единственный во втором; None — не найден или неоднозначен."""
    for members in (circle.primary, circle.extended):
        resolver = NameResolver(await _persons(members))
        if (found := resolver.short(name, members)) is not None:
            return found
        if resolver.matches(name, members):
            return None
    return None
