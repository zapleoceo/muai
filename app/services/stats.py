from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text

from app.db.database import AsyncSessionLocal
from app.db.models import Chat, Message, TgUser


async def get_dashboard_stats() -> dict:
    since = datetime.now(tz=timezone.utc) - timedelta(days=7)

    async with AsyncSessionLocal() as session:
        total_messages = (await session.execute(select(func.count()).select_from(Message))).scalar()
        total_chats    = (await session.execute(select(func.count()).select_from(Chat))).scalar()
        total_users    = (await session.execute(select(func.count()).select_from(TgUser))).scalar()

        daily_rows = (await session.execute(
            text("""
                SELECT date_trunc('day', date_utc) AS day, COUNT(*) AS cnt
                FROM messages WHERE date_utc >= :since
                GROUP BY day ORDER BY day
            """),
            {"since": since},
        )).all()

        top_chat_rows = (await session.execute(
            text("""
                SELECT c.title, c.type, COUNT(m.id) AS cnt
                FROM messages m JOIN chats c ON c.id = m.chat_id
                WHERE m.date_utc >= :since
                GROUP BY c.id, c.title, c.type
                ORDER BY cnt DESC LIMIT 10
            """),
            {"since": since},
        )).all()

        in_count  = (await session.execute(
            select(func.count()).select_from(Message).where(Message.direction == "in")
        )).scalar()
        out_count = (await session.execute(
            select(func.count()).select_from(Message).where(Message.direction == "out")
        )).scalar()

        db_size_row = (await session.execute(
            text("SELECT pg_size_pretty(pg_database_size(current_database())) AS total,"
                 "       pg_size_pretty(pg_total_relation_size('messages')) AS messages_tbl")
        )).one()

        total_chunks = (await session.execute(
            text("SELECT COUNT(*) FROM message_chunks")
        )).scalar()

        embedded_chats = (await session.execute(
            text("SELECT COUNT(DISTINCT chat_id) FROM message_chunks")
        )).scalar()

    return {
        "totals": {
            "messages": total_messages,
            "chats": total_chats,
            "users": total_users,
            "incoming": in_count,
            "outgoing": out_count,
            "db_size": db_size_row.total,
            "messages_size": db_size_row.messages_tbl,
            "chunks": total_chunks,
            "embedded_chats": embedded_chats,
        },
        "daily":     [{"day": str(r.day)[:10], "count": r.cnt} for r in daily_rows],
        "top_chats": [{"title": r.title or r.type, "type": r.type, "count": r.cnt} for r in top_chat_rows],
    }
