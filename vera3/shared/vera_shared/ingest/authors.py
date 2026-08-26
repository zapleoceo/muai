"""Автор события → сущность в графе. Один upsert на identifier за прогон.

Раньше это жило по копии в каждом ингесторе: у gmail `correspondent_of()` +
`sync_correspondent_entity()`, у trello `store.sync_authors()`, у telegram
`entity_sync`. Одинаковыми во всех трёх были ровно три вещи, и они здесь:
дедуп по identifier в пределах прогона, фиксированный `source` алиаса и
запрет ронять приём событий из-за сбоя графа.

Различалось то, как из метаданных источника достать саму сущность — это и
стало параметром `author_of`. Он возвращает kwargs для `upsert_entity`, а не
пару строк, потому что источники знают про автора разное: gmail различает
человека и организацию по адресу (`identity.entity_kind_for_email`) и несёт
`attributes={"email": …}`, trello — только username.

Слияние двойников между источниками здесь не делается вообще: этим занят
существующий /entities/duplicates, и второй системы дедупа быть не должно.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from vera_shared.graph.repo import upsert_entity, upsert_entity_linked

log = logging.getLogger(__name__)

#: спецификация события → kwargs `upsert_entity` без `source`, либо None если
#: автор — владелец или его не удалось определить. Обязателен `identifier`.
#: Необязательный `known_as` — список пар (source, identifier), которые источник
#: знает про этого человека помимо своей: тогда сущность не заводится заново, а
#: прицепляется к существующей. Slack, например, отдаёт в профиле рабочий email,
#: а он же служит алиасом gmail.
AuthorExtractor = Callable[[dict[str, Any]], dict[str, Any] | None]


async def sync_author_entities(
    specs: list[dict[str, Any]],
    *,
    source: str,
    author_of: AuthorExtractor,
) -> int:
    """Завести сущности для авторов событий. → сколько разных авторов задето."""
    seen: set[str] = set()
    linked = 0
    for spec in specs:
        entity = author_of(spec)
        if not entity:
            continue
        identifier = str(entity.get("identifier") or "").strip()
        if not identifier or identifier in seen:
            continue
        seen.add(identifier)
        fields = {"type": "person", **entity, "identifier": identifier}
        known_as = fields.pop("known_as", None)
        try:
            if known_as:
                _id, how = await upsert_entity_linked(
                    source=source, known_as=list(known_as), **fields)
                if how == "linked":
                    linked += 1
            else:
                await upsert_entity(source=source, **fields)
        except Exception as e:  # noqa: BLE001 — сбой графа не останавливает приём событий
            log.warning("%s: не завёл сущность для %s: %s", source, identifier, e)
    if linked:
        log.info("%s: %d авторов прицеплены к уже известным людям", source, linked)
    return len(seen)
