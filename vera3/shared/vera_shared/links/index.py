"""Индекс связей `event_entities` (миграция 042): сборка по пачкам событий.

Новые события (`forward`) обрабатывает шаг brain-triage, старые (`backfill`, от больших id к
меньшим) — скрипт `backfill_event_links.py`, резюмируемо: курсор в `link_cursor`. Пачка пишется
одной транзакцией: производные связи событий пачки удаляются и вставляются заново (ручные
не трогаются), поэтому повтор безопасен, а смена прозвищ, карты голосов или имён подхватывается
пересчётом (`--reset`). Событие, на котором сборка падает, пропускается с записью в лог — иначе
одно плохое событие остановило бы курсор навсегда; сбой самой базы (соединение, таймаут) —
не плохое событие и пробрасывается.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

from vera_shared.db.engine import get_session
from vera_shared.db.models import EventRow
from vera_shared.links.builders import base_links, mention_links
from vera_shared.links.context import ContextBuilder, EventFacts, EventView
from vera_shared.links.index_resources import Resources, load_resources
from vera_shared.links.index_store import (
    BACKFILL,
    FORWARD,
    load_views,
    max_event_id,
    next_batch,
    reset_cursors,
    set_cursor,
    view_of_row,
    write_links,
)
from vera_shared.links.index_voice import voice_guesses, voice_links_for
from vera_shared.links.lock import require_links_lock
from vera_shared.links.model import Link

__all__ = ["BACKFILL", "DEFAULT_BATCH", "FORWARD", "BatchResult", "Resources", "index_events",
           "index_views", "links_for", "load_resources", "max_event_id", "reindex_source",
           "reindex_token", "reindex_token_report", "Reindexed",
           "reset_cursors", "run_batch"]

log = logging.getLogger(__name__)

DEFAULT_BATCH = 500
#: Потолок пересчёта по токену прозвища; больше — полный `--reset`.
MAX_TOKEN_REINDEX = 5000
#: Эти сбои — база или сеть, а не плохое событие: пробрасываем, пачка повторится.
_INFRASTRUCTURE = (OperationalError, InterfaceError, OSError, TimeoutError)


@dataclass(frozen=True)
class BatchResult:
    events: int
    links: int
    last_id: int | None
    skipped: tuple[int, ...] = ()


def links_for(view: EventView, facts: EventFacts, owner: int | None, res: Resources,
              guessed: list | None = None) -> list[Link]:
    """Все связи одного события (чистая сборка; `guessed` — ASR-угадывания для созвона)."""
    if view.source == "voice":
        return voice_links_for(view, owner, res, guessed or [])
    found = res.matcher.find(view.text, facts.ctx, facts.author)
    return base_links(view, facts) + mention_links(view, found)


async def _links_of(views: list[EventView], res: Resources, builder: ContextBuilder) -> list[Link]:
    facts = await builder.build(views)
    links: list[Link] = []
    for view in views:
        guessed = await voice_guesses(view, res) if view.source == "voice" else None
        links += links_for(view, facts[view.id], builder.owner, res, guessed)
    return links


async def index_views(views: list[EventView], res: Resources, builder: ContextBuilder) -> int:
    """Пересобрать связи событий пачки; → число записанных строк."""
    visible = [v for v in views if not v.hidden]
    links = await _links_of(visible, res, builder) if visible else []
    return await write_links([v.id for v in views], links)


async def index_events(rows: list[EventRow], res: Resources, builder: ContextBuilder) -> int:
    """То же для полностью загруженных строк `EventRow`."""
    return await index_views([view_of_row(r) for r in rows], res, builder)


async def _index_tolerant(views: list[EventView], res: Resources,
                          builder: ContextBuilder) -> tuple[int, list[int]]:
    """Пачка целиком; при сбое на данных — по одному событию, плохие пропускаются."""
    try:
        return await index_views(views, res, builder), []
    except _INFRASTRUCTURE:
        raise
    except Exception:
        log.warning("links: пачка %d..%d не собралась, разбираю по одному", views[0].id,
                    views[-1].id, exc_info=True)
    total, skipped = 0, []
    for view in views:
        try:
            total += await index_views([view], res, builder)
        except _INFRASTRUCTURE:
            raise
        except Exception:
            log.warning("links: событие %s пропущено — сборка связей падает на нём", view.id,
                        exc_info=True)
            skipped.append(view.id)
    return total, skipped


async def _reindex_ids(ids: list[int], builder: ContextBuilder | None) -> int:
    if not ids:
        return 0
    builder = builder or ContextBuilder()
    await builder.preload()
    res = await load_resources(builder.owner)
    for start in range(0, len(ids), DEFAULT_BATCH):
        await _index_tolerant(await load_views(ids[start:start + DEFAULT_BATCH]), res, builder)
    return len(ids)


@dataclass(frozen=True)
class Reindexed:
    count: int
    #: Совпавших событий больше `limit`: старые остались без пересчёта (нужен `--reset`).
    truncated: bool


async def reindex_token_report(token: str, case_sensitive: bool = True,
                               limit: int = MAX_TOKEN_REINDEX,
                               builder: ContextBuilder | None = None) -> Reindexed:
    """Пересобрать связи событий, в тексте которых встречается `token` (после добавления
    прозвища): дёшево по сравнению с полным `--reset`. Берутся `limit` самых новых; `truncated` —
    были и старее. Идёт под замком цикла связей: занят — `LinksBusyError`."""
    column = EventRow.content_text
    match = column.contains(token, autoescape=True) if case_sensitive else column.icontains(
        token, autoescape=True)
    async with require_links_lock():
        async with get_session() as s:
            found = sorted((await s.execute(select(EventRow.id).where(match)
                                            .order_by(EventRow.id.desc()).limit(limit + 1))).scalars())
        ids = found[-limit:] if len(found) > limit else found
        return Reindexed(await _reindex_ids(ids, builder), len(found) > limit)


async def reindex_token(token: str, case_sensitive: bool = True, limit: int = MAX_TOKEN_REINDEX,
                        builder: ContextBuilder | None = None) -> int:
    """`reindex_token_report` без признака обрезки: → сколько событий пересчитано."""
    return (await reindex_token_report(token, case_sensitive, limit, builder)).count


async def reindex_source(source: str, limit: int = MAX_TOKEN_REINDEX) -> int:
    """Пересобрать связи всех событий источника (например `voice` — после правила разбора участников
    созвона или карты голосов): сотни событий, а не весь мозг. → число событий. Под замком цикла
    связей: занят — `LinksBusyError`."""
    async with require_links_lock():
        async with get_session() as s:
            ids = sorted((await s.execute(select(EventRow.id).where(EventRow.source == source)
                                          .order_by(EventRow.id.desc()).limit(limit))).scalars())
        return await _reindex_ids(ids, None)


async def run_batch(name: str, res: Resources, builder: ContextBuilder,
                    batch: int = DEFAULT_BATCH) -> BatchResult:
    """Одна пачка потока `name`; `last_id` None — делать больше нечего. Курсор двигается и
    тогда, когда часть событий пропущена (`skipped`)."""
    try:
        ids = await next_batch(name, batch)
    except DBAPIError as e:
        log.warning("event_entities не готова (миграция 042?): %s", e)
        return BatchResult(0, 0, None)
    if not ids:
        return BatchResult(0, 0, None)
    count, skipped = await _index_tolerant(await load_views(ids), res, builder)
    edge = ids[-1] if name == FORWARD else ids[0]
    await set_cursor(name, edge)
    return BatchResult(len(ids), count, edge, tuple(skipped))

