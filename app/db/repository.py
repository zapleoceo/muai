from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Chat, Message, Setting, TgUser


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
