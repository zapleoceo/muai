from sqlalchemy import select, text

from app.db.models import MessageChunk


class ChunkRepo:
    async def get_last_embedded_msg_id(self, chat_id: int) -> int | None:
        q = (
            select(MessageChunk.max_msg_id)
            .where(MessageChunk.chat_id == chat_id, MessageChunk.max_msg_id.isnot(None))
            .order_by(MessageChunk.max_msg_id.desc())
            .limit(1)
        )
        return (await self.session.execute(q)).scalar_one_or_none()

    async def chunk_stats(self) -> dict:
        total = (await self.session.execute(text("SELECT COUNT(*) FROM message_chunks"))).scalar()
        pending = (await self.session.execute(
            text("""
                SELECT COUNT(*) FROM messages m
                LEFT JOIN (
                    SELECT chat_id, MAX(max_msg_id) AS last_id
                    FROM message_chunks GROUP BY chat_id
                ) lc ON lc.chat_id = m.chat_id
                WHERE (m.text IS NOT NULL OR m.caption IS NOT NULL)
                  AND m.id > COALESCE(lc.last_id, 0)
            """)
        )).scalar()
        pending_by_chat = (await self.session.execute(
            text("""
                WITH lc AS (
                    SELECT chat_id, MAX(max_msg_id) AS last_id
                    FROM message_chunks
                    GROUP BY chat_id
                )
                SELECT c.id AS chat_id, c.title AS title, c.type AS chat_type, COUNT(m.id) AS pending
                FROM messages m
                JOIN chats c ON c.id = m.chat_id
                LEFT JOIN lc ON lc.chat_id = m.chat_id
                WHERE (m.text IS NOT NULL OR m.caption IS NOT NULL)
                  AND m.id > COALESCE(lc.last_id, 0)
                GROUP BY c.id, c.title, c.type
                ORDER BY pending DESC
                LIMIT 30
            """)
        )).fetchall()
        per_chat = (await self.session.execute(
            text("""
                SELECT mc.chat_id, c.title, COUNT(*) AS cnt,
                       MAX(mc.max_msg_id) AS last_msg_id
                FROM message_chunks mc
                LEFT JOIN chats c ON c.id = mc.chat_id
                GROUP BY mc.chat_id, c.title
                ORDER BY cnt DESC
                LIMIT 20
            """)
        )).fetchall()
        return {
            "total_chunks": total,
            "messages_pending": pending,
            "pending_by_chat": [{"chat_id": r.chat_id, "title": r.title, "chat_type": r.chat_type, "pending": r.pending} for r in pending_by_chat],
            "per_chat": [{"chat_id": r.chat_id, "title": r.title, "chunks": r.cnt, "last_msg_id": r.last_msg_id} for r in per_chat],
        }

    async def search_chunks(
        self,
        embedding: list[float],
        limit: int = 12,
        chat_id: int | None = None,
        chat_ids: list[int] | None = None,
    ):
        vec_str = "[" + ",".join(str(x) for x in embedding) + "]"
        where = "WHERE embedding IS NOT NULL"
        params: dict[str, object] = {"vec": vec_str, "lim": limit}
        if chat_id is not None:
            where += " AND chat_id = :chat_id"
            params["chat_id"] = chat_id
        elif chat_ids:
            where += " AND chat_id = ANY(CAST(:chat_ids AS bigint[]))"
            params["chat_ids"] = chat_ids
        rows = (await self.session.execute(
            text(
                "SELECT id, chat_id, chat_title, chunk_text, msg_date_from, msg_date_to, chat_username, max_tg_msg_id, min_msg_id, max_msg_id, msg_count, meta, "
                "(embedding <=> CAST(:vec AS vector)) AS distance "
                "FROM message_chunks "
                f"{where} "
                "ORDER BY distance "
                "LIMIT :lim"
            ),
            params,
        )).fetchall()
        return rows

    async def search_media_chunks(
        self,
        embedding: list[float],
        limit: int = 12,
        chat_id: int | None = None,
        chat_ids: list[int] | None = None,
    ):
        vec_str = "[" + ",".join(str(x) for x in embedding) + "]"
        where = "WHERE embedding IS NOT NULL"
        params: dict[str, object] = {"vec": vec_str, "lim": limit}
        if chat_id is not None:
            where += " AND chat_id = :chat_id"
            params["chat_id"] = chat_id
        elif chat_ids:
            where += " AND chat_id = ANY(CAST(:chat_ids AS bigint[]))"
            params["chat_ids"] = chat_ids
        rows = (await self.session.execute(
            text(
                "SELECT id, chat_id, chat_title, chat_username, source_tg_msg_id, media_type, date_utc, chunk_text, meta, "
                "(embedding <=> CAST(:vec AS vector)) AS distance "
                "FROM media_chunks "
                f"{where} "
                "ORDER BY distance "
                "LIMIT :lim"
            ),
            params,
        )).fetchall()
        return rows
