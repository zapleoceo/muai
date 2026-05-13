from sqlalchemy import delete, func, select

from app.db.database import AsyncSessionLocal
from app.db.models import Chat, ChatSyncConfig, ChatTopic, Message


class ChatQueryService:
    async def list_chats_with_config(self) -> list[dict]:
        async with AsyncSessionLocal() as session:
            msg_count_sub = (
                select(Message.chat_id, func.count(Message.id).label("cnt"))
                .group_by(Message.chat_id)
                .subquery()
            )
            rows = (await session.execute(
                select(Chat, ChatSyncConfig, msg_count_sub.c.cnt)
                .outerjoin(ChatSyncConfig, Chat.id == ChatSyncConfig.chat_id)
                .outerjoin(msg_count_sub, Chat.id == msg_count_sub.c.chat_id)
                .order_by(Chat.title)
            )).all()

            topic_rows = (await session.execute(
                select(ChatTopic).order_by(ChatTopic.chat_id, ChatTopic.topic_id)
            )).scalars().all()

        topics_by_chat: dict[int, list[dict]] = {}
        for t in topic_rows:
            topics_by_chat.setdefault(t.chat_id, []).append({
                "id": t.topic_id,
                "title": t.title,
                "is_closed": t.is_closed,
            })

        result = []
        for chat, cfg, msg_cnt in rows:
            if cfg is None:
                status = "unknown"
            elif cfg.enabled:
                status = "active"
            elif cfg.approved_at is None:
                status = "pending"
            else:
                status = "disabled"

            result.append({
                "id": chat.id,
                "type": chat.type,
                "title": chat.title or str(chat.id),
                "username": chat.username,
                "folder": chat.folder,
                "status": status,
                "enabled": cfg.enabled if cfg else False,
                "depth_days": cfg.depth_days if cfg else None,
                "skip_reason": cfg.skip_reason if cfg else None,
                "approved_at": cfg.approved_at.isoformat() if cfg and cfg.approved_at else None,
                "last_synced_at": cfg.last_synced_at.isoformat() if cfg and cfg.last_synced_at else None,
                "message_count": msg_cnt or 0,
                "topics": topics_by_chat.get(chat.id, []),
            })
        return result

    async def delete_chat_messages(self, chat_id: int) -> int:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                delete(Message).where(Message.chat_id == chat_id)
            )
            await session.commit()
            return result.rowcount

