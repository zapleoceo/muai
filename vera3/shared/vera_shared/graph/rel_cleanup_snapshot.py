"""Срез связей для чистки: все строки с концами, события-источники и имена концов.

Тот же срез собирает SELECT-выгрузка с прода (`EXPORT_SQL` в psql), поэтому
план можно составить без подключения скрипта к базе.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text

from vera_shared.db.engine import get_session
from vera_shared.graph.repo import find_entity_by_alias
from vera_shared.graph.repo_relationships import alias_index, entity_names

PAGE = 2000

_BASE_COLUMNS = """r.id, r.subject_entity_id, r.predicate, r.object_entity_id, r.confidence,
           r.fact, r.is_current, r.derived_from_event_id,
           es.name AS subject_name, es.type AS subject_type,
           eo.name AS object_name, eo.type AS object_type"""
_FROM = """FROM relationships r
    JOIN entities es ON es.id = r.subject_entity_id
    JOIN entities eo ON eo.id = r.object_entity_id
    LEFT JOIN events ev ON ev.id = r.derived_from_event_id"""

ROWS_SQL = (f"SELECT {_BASE_COLUMNS}, ev.source AS event_source, ev.metadata AS event_meta "
            f"{_FROM} WHERE r.id > :after ORDER BY r.id LIMIT :lim")
# Из метаданных события нужны только поля, по которым `resolve_author` находит автора.
_AUTHOR_META = ("jsonb_build_object('direction', ev.metadata::jsonb->>'direction', "
                "'author_role', ev.metadata::jsonb->>'author_role', "
                "'sender_id', ev.metadata::jsonb->>'sender_id', "
                "'from', ev.metadata::jsonb->>'from', "
                "'author_username', ev.metadata::jsonb->>'author_username')")
_ENDS_SQL = """SELECT subject_entity_id FROM relationships WHERE is_current
               UNION SELECT object_entity_id FROM relationships WHERE is_current"""

# psql -v owner=<OWNER_TELEGRAM_ID> -At -f; только SELECT.
EXPORT_SQL = f"""
SELECT json_build_object(
  'owner_id', (SELECT entity_id FROM entity_aliases
               WHERE source = 'telegram' AND identifier = 'user:' || :'owner'),
  'relationships', (SELECT json_agg(t) FROM (
      SELECT {_BASE_COLUMNS}, ev.source AS event_source, {_AUTHOR_META} AS event_meta
      {_FROM} ORDER BY r.id) t),
  'aliases', (SELECT json_object_agg(a.source || ':' || a.identifier, a.entity_id)
              FROM entity_aliases a WHERE a.entity_id IN ({_ENDS_SQL})),
  'names', (SELECT json_object_agg(id, n) FROM (
      SELECT e.id, array_remove(array_agg(DISTINCT v.x), NULL) AS n
      FROM entities e
      LEFT JOIN entity_aliases a ON a.entity_id = e.id
      CROSS JOIN LATERAL (VALUES (e.name), (a.display_name)) v(x)
      WHERE e.id IN ({_ENDS_SQL})
      GROUP BY e.id) q));
"""


async def load_snapshot(owner_tg_id: int) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    after = 0
    while True:
        async with get_session() as s:
            page = [dict(r) for r in (await s.execute(
                text(ROWS_SQL), {"after": after, "lim": PAGE})).mappings().all()]
        if not page:
            break
        rows += page
        after = page[-1]["id"]
    ends = sorted({r[k] for r in rows if r["is_current"]
                   for k in ("subject_entity_id", "object_entity_id")})
    names = {str(i): n for i, n in (await entity_names(ends)).items()}
    owner = await find_entity_by_alias("telegram", f"user:{owner_tg_id}")
    return {"owner_id": owner, "relationships": rows, "names": names,
            "aliases": await alias_index(ends)}
