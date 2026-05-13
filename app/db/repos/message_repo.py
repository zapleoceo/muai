from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.models import Message, TgUser


class MessageOpsRepo:
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

    async def get_recent_messages_with_users(
        self, chat_id: int, limit: int = 60
    ) -> list[tuple[Message, TgUser | None]]:
        q = (
            select(Message, TgUser)
            .outerjoin(TgUser, Message.user_id == TgUser.id)
            .where(Message.chat_id == chat_id)
            .order_by(Message.date_utc.desc())
            .limit(limit)
        )
        rows = (await self.session.execute(q)).all()
        return list(reversed(rows))

    async def get_messages_after_with_users(
        self, chat_id: int, after_id: int | None
    ) -> list[tuple[Message, TgUser | None]]:
        q = (
            select(Message, TgUser)
            .outerjoin(TgUser, Message.user_id == TgUser.id)
            .where(Message.chat_id == chat_id)
        )
        if after_id is not None:
            q = q.where(Message.id > after_id)
        q = q.order_by(Message.id.asc())
        return list((await self.session.execute(q)).all())
