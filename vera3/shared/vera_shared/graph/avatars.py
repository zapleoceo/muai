"""Entity avatar storage — read/write primitives over `entity_avatars`.

Kept separate from `repo.py` (identity graph) and `dedup.py` (merge logic):
one responsibility — the profile-photo blob side-table. The dashboard reads
via `get_avatar`; the throttled userbot backfill writes via `upsert_avatar`
and picks work via `list_entities_needing_avatar`.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import bindparam, text

from vera_shared.db.engine import get_session


async def get_avatar(entity_id: int) -> tuple[bytes, str] | None:
    """(image_bytes, mime) for an entity, or None if absent/known-missing."""
    async with get_session() as s:
        row = (await s.execute(text(
            "SELECT image, mime FROM entity_avatars "
            "WHERE entity_id = :eid AND image IS NOT NULL"
        ), {"eid": entity_id})).first()
    if row is None:
        return None
    return bytes(row[0]), row[1]


async def upsert_avatar(
    entity_id: int, *, image: bytes | None, mime: str = "image/jpeg",
    missing: bool = False,
) -> None:
    """Store a fetched photo, or mark the entity as checked-but-photoless
    (`missing=True`, `image=None`) so backfill skips it next round."""
    async with get_session() as s:
        exists = (await s.execute(text(
            "SELECT 1 FROM entity_avatars WHERE entity_id = :eid"
        ), {"eid": entity_id})).first()
        params = {"eid": entity_id, "img": image, "mime": mime,
                  "missing": missing, "ts": datetime.utcnow()}
        if exists:
            await s.execute(text(
                "UPDATE entity_avatars SET image=:img, mime=:mime, "
                "missing=:missing, fetched_at=:ts WHERE entity_id=:eid"
            ), params)
        else:
            await s.execute(text(
                "INSERT INTO entity_avatars (entity_id, image, mime, missing, fetched_at) "
                "VALUES (:eid, :img, :mime, :missing, :ts)"
            ), params)


async def list_entities_needing_avatar(
    limit: int = 50, *, ids: list[int] | None = None,
) -> list[dict]:
    """Telegram person/channel entities with a tg_id and no avatar row yet,
    highest-degree first (most-seen people matter most). `ids` restricts to a
    specific set (used to prioritise entities visible on the dedup page)."""
    id_clause = "AND e.id IN :ids" if ids else ""
    q = text(f"""
        SELECT e.id, e.type,
               e.attributes->>'username' AS username,
               e.attributes->>'tg_id'    AS tg_id,
               (SELECT COUNT(*) FROM relationships r
                  WHERE r.subject_entity_id = e.id
                     OR r.object_entity_id = e.id) AS degree
        FROM entities e
        LEFT JOIN entity_avatars av ON av.entity_id = e.id
        WHERE e.type IN ('person', 'channel', 'supergroup', 'group')
          AND e.attributes->>'tg_id' IS NOT NULL
          AND av.entity_id IS NULL
          {id_clause}
        ORDER BY degree DESC
        LIMIT :lim
    """)
    if ids:
        q = q.bindparams(bindparam("ids", expanding=True))
    async with get_session() as s:
        rows = (await s.execute(q, {"lim": limit, **({"ids": ids} if ids else {})})).mappings().all()
    return [dict(r) for r in rows]
