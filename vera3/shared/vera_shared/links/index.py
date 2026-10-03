"""Индекс связей `event_entities` (миграция 042): сборка по пачкам событий.

Новые события (`forward`) обрабатывает шаг brain-triage, старые (`backfill`, от
больших id к меньшим) — скрипт `backfill_event_links.py`, резюмируемо: курсор в
`link_cursor`. Пачка пишется одной транзакцией: связи событий пачки сначала
удаляются, потом вставляются, поэтому повтор безопасен, а смена прозвищ, карты голосов
или имён подхватывается пересчётом (`--reset`). Чтение терпит отсутствие таблиц.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError

from vera_shared.db.engine import get_session
from vera_shared.db.models import EventRow
from vera_shared.db.models_links import EventEntityRow, LinkCursorRow
from vera_shared.events.visibility import HIDDEN_STATUS
from vera_shared.ingest.envelope import message_body
from vera_shared.links.asr import asr_matches
from vera_shared.links.builders import base_links, is_anonymous, mention_links, voice_links
from vera_shared.links.context import (
    ContextBuilder,
    EventFacts,
    EventView,
    established_contacts,
)
from vera_shared.links.matcher import MentionMatcher, PersonNames
from vera_shared.links.model import MANUAL, MENTIONED, NAME_MATCH, VOICEPRINT, Link
from vera_shared.links.names import NameResolver
from vera_shared.links.nicknames import active_rules

log = logging.getLogger(__name__)

FORWARD, BACKFILL = "forward", "backfill"
DEFAULT_BATCH = 500
MAX_TEXT_CHARS = 4000
MAX_VOICE_CHARS = 60_000
FULL_NAME_CONFIDENCE, SHORT_NAME_CONFIDENCE = 0.9, 0.6


@dataclass(frozen=True)
class BatchResult:
    events: int
    links: int
    last_id: int | None


@dataclass
class Resources:
    """Всё, что нужно сборке и что меняется редко: перечитывается на каждый прогон."""
    matcher: MentionMatcher
    names: NameResolver
    owner_circle: frozenset[int]
    speaker_map: dict[tuple[str, str], int]
    asr_candidates: dict[int, str]


async def load_resources(owner: int | None) -> Resources:
    async with get_session() as s:
        rows = (await s.execute(text(
            "SELECT id, name, attributes->>'username' FROM entities WHERE type = 'person'"))).all()
        try:
            mapped = (await s.execute(text(
                "SELECT kind, key, entity_id FROM voice_speaker_map"))).all()
        except DBAPIError as e:
            log.warning("voice_speaker_map не прочитана (миграция 044?): %s", e)
            mapped = []
    persons = [PersonNames(r[0], r[1], (r[2],) if r[2] else ()) for r in rows if r[1]]
    rules = await active_rules()
    strong = {e: await established_contacts(e) for e in {r.entity_id for r in rules}}
    circle = await established_contacts(owner) | ({owner} if owner is not None else frozenset())
    return Resources(MentionMatcher(persons, rules, strong), NameResolver(persons),
                     frozenset(circle), {(r[0], r[1]): r[2] for r in mapped},
                     {p.entity_id: p.name for p in persons if p.entity_id in circle})


def _view(row: EventRow) -> EventView:
    return EventView(row.id, row.source, message_body(row.content_text)[:MAX_TEXT_CHARS],
                     row.metadata_ or {}, row.content_extra if isinstance(row.content_extra, dict)
                     else None, row.transcript_text, row.occurred_at)


def _voiceprints(view: EventView) -> dict[str, str]:
    """ярлык говорящего → id отпечатка, если слушатель его прислал (будущее поле реплики)."""
    return {str(u["speaker"]): str(u["voiceprint"])
            for u in (view.extra or {}).get("utterances") or []
            if u.get("speaker") and u.get("voiceprint")}


def _voice(view: EventView, owner: int | None, res: Resources) -> list[Link]:
    prints = _voiceprints(view)

    def speaker(label: str) -> tuple[int, str, float] | None:
        if (eid := res.speaker_map.get(("event", f"{view.id}:{label}"))) is not None:
            return eid, MANUAL, 1.0
        if label in prints and (eid := res.speaker_map.get(("voiceprint", prints[label]))):
            return eid, VOICEPRINT, 1.0
        found = None if is_anonymous(label) else res.names.resolve(label, res.owner_circle)
        if found is None:
            return None
        confidence = FULL_NAME_CONFIDENCE if found[1] == "full" else SHORT_NAME_CONFIDENCE
        return found[0], VOICEPRINT if found[1] == "full" else NAME_MATCH, confidence

    def counterpart(name: str) -> int | None:
        found = res.names.resolve(name, res.owner_circle)
        return found[0] if found else None

    body = (view.transcript or view.text)[:MAX_VOICE_CHARS]
    exact = res.matcher.find(body, EventFacts().ctx, owner, nicknames=False)
    guessed = asr_matches(body, res.asr_candidates)
    known = {m.entity_id for m in exact}
    links = voice_links(view, owner, speaker, counterpart,
                        [m for m in guessed if m.entity_id not in known])
    return links + [Link(view.id, m.entity_id, MENTIONED, NAME_MATCH, m.confidence, m.token[:120])
                    for m in exact]


def links_for(view: EventView, facts: EventFacts, owner: int | None, res: Resources) -> list[Link]:
    """Все связи одного события (чистая сборка поверх загруженных ресурсов)."""
    if view.source == "voice":
        return _voice(view, owner, res)
    found = res.matcher.find(view.text, facts.ctx, facts.author)
    return base_links(view, facts) + mention_links(view, found)


def _dedupe(links: list[Link]) -> list[EventEntityRow]:
    best: dict[tuple[int, int, str, str], Link] = {}
    for link in links:
        key = (link.event_id, link.entity_id, link.role, link.token)
        if key not in best or best[key].confidence < link.confidence:
            best[key] = link
    return [EventEntityRow(event_id=k.event_id, entity_id=k.entity_id, role=k.role,
                           token=k.token, source_of_link=k.source, confidence=k.confidence,
                           span=k.span, scope_ok=k.scope_ok) for k in best.values()]


async def index_events(rows: list[EventRow], res: Resources, builder: ContextBuilder) -> int:
    """Пересобрать связи событий пачки; → число записанных строк."""
    views = [_view(r) for r in rows if r.triage_status != HIDDEN_STATUS]
    facts = await builder.build(views)
    links: list[Link] = []
    for view in views:
        links += links_for(view, facts[view.id], builder.owner, res)
    found = _dedupe(links)
    async with get_session() as s:
        await s.execute(delete(EventEntityRow).where(
            EventEntityRow.event_id.in_([r.id for r in rows])))
        s.add_all(found)
    return len(found)


async def reindex_event(event_id: int) -> int:
    """Пересобрать связи одного события (после решения владельца по голосу)."""
    async with get_session() as s:
        row = await s.get(EventRow, event_id)
    if row is None:
        return 0
    builder = ContextBuilder()
    await builder.build([])
    return await index_events([row], await load_resources(builder.owner), builder)


async def _cursor(name: str) -> int | None:
    async with get_session() as s:
        row = await s.get(LinkCursorRow, name)
    return row.event_id if row else None


async def _set_cursor(name: str, event_id: int) -> None:
    async with get_session() as s:
        row = await s.get(LinkCursorRow, name)
        if row is None:
            s.add(LinkCursorRow(name=name, event_id=event_id))
        else:
            row.event_id = event_id


async def max_event_id() -> int:
    async with get_session() as s:
        return int((await s.execute(select(EventRow.id).order_by(EventRow.id.desc()).limit(1)))
                   .scalar_one_or_none() or 0)


async def reset_cursors() -> int:
    """Начать заново: forward — с конца, backfill — от конца вниз. → текущий max(id)."""
    top = await max_event_id()
    await _set_cursor(FORWARD, top)
    await _set_cursor(BACKFILL, top + 1)
    return top


async def _next_rows(name: str, batch: int) -> list[EventRow]:
    cursor = await _cursor(name)
    if cursor is None:
        cursor = await max_event_id() + (1 if name == BACKFILL else 0)
        await _set_cursor(name, cursor)
    query = select(EventRow)
    query = (query.where(EventRow.id > cursor).order_by(EventRow.id).limit(batch)
             if name == FORWARD else
             query.where(EventRow.id < cursor).order_by(EventRow.id.desc()).limit(batch))
    async with get_session() as s:
        return list((await s.execute(query)).scalars())


async def run_batch(name: str, res: Resources, builder: ContextBuilder,
                    batch: int = DEFAULT_BATCH) -> BatchResult:
    """Одна пачка потока `name`; `last_id` None — делать больше нечего."""
    try:
        rows = await _next_rows(name, batch)
    except DBAPIError as e:
        log.warning("event_entities не готова (миграция 042?): %s", e)
        return BatchResult(0, 0, None)
    if not rows:
        return BatchResult(0, 0, None)
    count = await index_events(rows, res, builder)
    edge = rows[-1].id
    await _set_cursor(name, edge)
    return BatchResult(len(rows), count, edge)
