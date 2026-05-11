from datetime import datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Chat, Message, MessageChunk, Setting, TgUser


class MessageRepo:
    def __init__(self, session: AsyncSession):
        self.session = session

    # ── chats ─────────────────────────────────────────────────────────────────

    async def upsert_chat(self, chat) -> None:
        """Accept an aiogram Chat object."""
        await self.upsert_chat_raw(
            id=chat.id,
            type=str(chat.type.value) if hasattr(chat.type, "value") else str(chat.type),
            title=getattr(chat, "title", None) or getattr(chat, "first_name", None),
            username=getattr(chat, "username", None),
        )

    async def upsert_chat_raw(
        self,
        *,
        id: int,
        type: str,
        title: str | None = None,
        username: str | None = None,
    ) -> None:
        stmt = (
            insert(Chat)
            .values(id=id, type=type, title=title, username=username)
            .on_conflict_do_update(
                index_elements=["id"],
                set_={"type": type, "title": title, "username": username},
            )
        )
        await self.session.execute(stmt)

    # ── users ─────────────────────────────────────────────────────────────────

    async def upsert_user(self, user) -> None:
        """Accept an aiogram User object."""
        await self.upsert_user_raw(
            id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            language_code=getattr(user, "language_code", None),
            is_bot=user.is_bot,
        )

    async def upsert_user_raw(
        self,
        *,
        id: int,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        language_code: str | None = None,
        is_bot: bool = False,
    ) -> None:
        stmt = (
            insert(TgUser)
            .values(
                id=id,
                username=username,
                first_name=first_name,
                last_name=last_name,
                language_code=language_code,
                is_bot=is_bot,
            )
            .on_conflict_do_update(
                index_elements=["id"],
                set_={"username": username, "first_name": first_name, "last_name": last_name},
            )
        )
        await self.session.execute(stmt)

    # ── messages ──────────────────────────────────────────────────────────────

    async def save_message(
        self,
        *,
        chat_id: int,
        user_id: int | None,
        telegram_msg_id: int | None,
        direction: str,
        text: str | None = None,
        media_type: str | None = None,
        file_id: str | None = None,
        caption: str | None = None,
        raw_json: dict[str, Any] | None = None,
        date_utc: datetime | None = None,
        reply_to_msg_id: int | None = None,
        is_auto_reply: bool = False,
        via_guest_bot: bool = False,
        edit_date: datetime | None = None,
        dialog_key: str | None = None,
    ) -> Message | None:
        msg = Message(
            chat_id=chat_id,
            user_id=user_id,
            telegram_msg_id=telegram_msg_id,
            direction=direction,
            text=text,
            media_type=media_type,
            file_id=file_id,
            caption=caption,
            raw_json=raw_json,
            date_utc=date_utc,
            reply_to_msg_id=reply_to_msg_id,
            is_auto_reply=is_auto_reply,
            via_guest_bot=via_guest_bot,
            edit_date=edit_date,
            dialog_key=dialog_key,
        )
        self.session.add(msg)
        try:
            await self.session.flush()
        except IntegrityError:
            await self.session.rollback()
            return None
        return msg

    async def get_messages(
        self,
        chat_id: int,
        limit: int = 50,
        offset: int = 0,
        direction: str | None = None,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
    ) -> list[Message]:
        q = select(Message).where(Message.chat_id == chat_id)
        if direction:
            q = q.where(Message.direction == direction)
        if from_date:
            q = q.where(Message.date_utc >= from_date)
        if to_date:
            q = q.where(Message.date_utc <= to_date)
        q = q.order_by(Message.date_utc.asc()).limit(limit).offset(offset)
        return list((await self.session.execute(q)).scalars().all())

    async def list_all_chats(self) -> list[Chat]:
        q = select(Chat).order_by(Chat.title)
        return list((await self.session.execute(q)).scalars().all())

    async def get_recent_messages_with_users(
        self, chat_id: int, limit: int = 60
    ) -> list[tuple[Message, TgUser | None]]:
        """Get recent messages for a chat, joined with sender info."""
        q = (
            select(Message, TgUser)
            .outerjoin(TgUser, Message.user_id == TgUser.id)
            .where(Message.chat_id == chat_id)
            .order_by(Message.date_utc.desc())
            .limit(limit)
        )
        rows = (await self.session.execute(q)).all()
        return list(reversed(rows))  # chronological order

    # ── chunks / vector search ────────────────────────────────────────────────

    async def get_last_embedded_msg_id(self, chat_id: int) -> int | None:
        """Return the highest message.id already covered by any chunk for this chat."""
        q = (
            select(MessageChunk.max_msg_id)
            .where(MessageChunk.chat_id == chat_id, MessageChunk.max_msg_id.isnot(None))
            .order_by(MessageChunk.max_msg_id.desc())
            .limit(1)
        )
        return (await self.session.execute(q)).scalar_one_or_none()

    async def get_messages_after_with_users(
        self, chat_id: int, after_id: int | None
    ) -> list[tuple[Message, TgUser | None]]:
        """Return messages (with sender) for a chat, optionally only those after after_id."""
        q = (
            select(Message, TgUser)
            .outerjoin(TgUser, Message.user_id == TgUser.id)
            .where(Message.chat_id == chat_id)
        )
        if after_id is not None:
            q = q.where(Message.id > after_id)
        q = q.order_by(Message.id.asc())
        return list((await self.session.execute(q)).all())

    async def chunk_stats(self) -> dict:
        """Return total chunks and per-chat chunk counts."""
        total = (await self.session.execute(
            text("SELECT COUNT(*) FROM message_chunks")
        )).scalar()
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
            "per_chat": [{"chat_id": r.chat_id, "title": r.title, "chunks": r.cnt, "last_msg_id": r.last_msg_id} for r in per_chat],
        }

    async def search_chunks(
        self, embedding: list[float], limit: int = 12
    ) -> list[MessageChunk]:
        """Return the most similar chunks across all chats using cosine distance."""
        vec_str = "[" + ",".join(str(x) for x in embedding) + "]"
        rows = (await self.session.execute(
            text(
                "SELECT id, chat_id, chat_title, chunk_text, msg_date_from, msg_date_to "
                "FROM message_chunks "
                "WHERE embedding IS NOT NULL "
                "ORDER BY embedding <=> CAST(:vec AS vector) "
                "LIMIT :lim"
            ),
            {"vec": vec_str, "lim": limit},
        )).fetchall()
        # Return as simple namedtuple-like rows
        return rows  # type: ignore[return-value]

    # ── settings ──────────────────────────────────────────────────────────────

    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = (await self.session.execute(
            select(Setting).where(Setting.key == key)
        )).scalar_one_or_none()
        return row.value if row else default

    async def set_setting(self, key: str, value: str) -> None:
        stmt = (
            insert(Setting)
            .values(key=key, value=value)
            .on_conflict_do_update(index_elements=["key"], set_={"value": value})
        )
        await self.session.execute(stmt)
