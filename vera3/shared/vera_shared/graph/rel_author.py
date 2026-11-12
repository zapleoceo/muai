"""Автор события как сущность графа — цель self-токенов («я») в rel_extract."""
from __future__ import annotations

import json

from sqlalchemy import text

from vera_shared.db.engine import get_session
from vera_shared.events.visibility import NOT_HIDDEN_SQL
from vera_shared.graph.repo import find_entity_by_alias
from vera_shared.ingest.authorship import OWNER, resolve_author


async def author_entity_of_event(event_id: int) -> int | None:
    """Кто автор события — как entity_id. Это цель self-токенов («я»).

    Решение «чей это текст» вынесено в таблицу `ingest.authorship`: раньше тут
    была цепочка `if source == …`, и источник без ветки целиком приписывался
    владельцу — тихая порча графа при каждом новом источнике.

    None, если автор не определён или его сущности (ещё) нет в графе: тогда
    self-связь скипается, а не вешается на кого попало.
    """
    from vera_shared.projects.rules import OWNER_TG_ID
    async with get_session() as s:
        row = (await s.execute(text(
            f"SELECT source, metadata FROM events WHERE id = :i AND {NOT_HIDDEN_SQL}"
        ), {"i": event_id})).first()
    if row is None:
        return None
    source, meta = row[0], row[1] or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            meta = {}

    author = resolve_author(source, meta)
    if author is None:
        return None
    if author is OWNER:
        return await find_entity_by_alias("telegram", f"user:{OWNER_TG_ID}")
    return await find_entity_by_alias(*author)

