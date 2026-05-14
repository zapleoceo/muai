import time

from sqlalchemy import text

from app.db.database import AsyncSessionLocal

_cache: dict = {}
_cache_ts: float = 0.0
_TTL = 30.0


async def get_dashboard_stats() -> dict:
    global _cache, _cache_ts
    now = time.monotonic()
    if _cache and (now - _cache_ts) < _TTL:
        return _cache
    result = await _compute_stats()
    _cache = result
    _cache_ts = now
    return result


async def _compute_stats() -> dict:
    async with AsyncSessionLocal() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))

        # All scalar counts in one round-trip
        totals_row = (await session.execute(text("""
            SELECT
              (SELECT COUNT(*) FROM messages)                          AS total_messages,
              (SELECT COUNT(*) FROM messages WHERE direction='in')     AS incoming,
              (SELECT COUNT(*) FROM messages WHERE direction='out')    AS outgoing,
              (SELECT COUNT(*) FROM chats)                            AS total_chats,
              (SELECT COUNT(*) FROM tg_users)                         AS total_users,
              pg_size_pretty(pg_database_size(current_database()))    AS db_size,
              pg_size_pretty(pg_total_relation_size('messages'))      AS messages_size,
              (SELECT COUNT(*) FROM message_chunks)                   AS total_chunks,
              (SELECT COUNT(DISTINCT chat_id) FROM message_chunks)    AS embedded_chats,
              (SELECT COUNT(*) FROM media_chunks)                     AS media_chunks,
              (SELECT COUNT(DISTINCT chat_id) FROM media_chunks)      AS media_embedded_chats
        """))).one()

        since_7d = "NOW() - INTERVAL '7 days'"
        daily_rows = (await session.execute(text(f"""
            SELECT date_trunc('day', date_utc)::date AS day, COUNT(*) AS cnt
            FROM messages WHERE date_utc >= {since_7d}
            GROUP BY 1 ORDER BY 1
        """))).all()

        top_rows = (await session.execute(text(f"""
            SELECT c.title, c.type, COUNT(m.id) AS cnt
            FROM messages m JOIN chats c ON c.id = m.chat_id
            WHERE m.date_utc >= {since_7d}
            GROUP BY c.id, c.title, c.type
            ORDER BY cnt DESC LIMIT 10
        """))).all()

    return {
        "totals": {
            "messages":             int(totals_row.total_messages or 0),
            "chats":                int(totals_row.total_chats or 0),
            "users":                int(totals_row.total_users or 0),
            "incoming":             int(totals_row.incoming or 0),
            "outgoing":             int(totals_row.outgoing or 0),
            "db_size":              totals_row.db_size,
            "messages_size":        totals_row.messages_size,
            "chunks":               int(totals_row.total_chunks or 0),
            "embedded_chats":       int(totals_row.embedded_chats or 0),
            "media_chunks":         int(totals_row.media_chunks or 0),
            "media_embedded_chats": int(totals_row.media_embedded_chats or 0),
        },
        "daily":     [{"day": str(r.day)[:10], "count": r.cnt} for r in daily_rows],
        "top_chats": [{"title": r.title or r.type, "type": r.type, "count": r.cnt} for r in top_rows],
    }
