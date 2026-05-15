from __future__ import annotations

from sqlalchemy import select, text

from app.db.database import AsyncSessionLocal
from app.db.models import Chat, Message
from app.db.repository import MessageRepo
from app.services.plan_executor.links import build_message_link


async def tool_get_recent_dialog(*, chat_id: int, limit: int) -> tuple[list[dict], dict]:
    async with AsyncSessionLocal() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        chat = (await session.execute(select(Chat).where(Chat.id == chat_id))).scalar_one_or_none()
        rows = await MessageRepo(session).get_recent_messages_with_users(chat_id=chat_id, limit=limit)
    items = []
    for (m, u) in rows:
        chat_username = getattr(chat, "username", None) if chat else None
        chat_type = getattr(chat, "type", None) if chat else None
        link = build_message_link(
            chat_id=int(m.chat_id),
            chat_type=chat_type,
            chat_username=chat_username,
            telegram_msg_id=int(m.telegram_msg_id) if m.telegram_msg_id is not None else None,
        )
        items.append({
            "chat_id": int(m.chat_id),
            "chat": {
                "id": int(m.chat_id),
                "type": chat_type,
                "title": getattr(chat, "title", None) if chat else None,
                "username": chat_username,
            },
            "message_id": int(m.id),
            "telegram_msg_id": int(m.telegram_msg_id) if m.telegram_msg_id is not None else None,
            "direction": m.direction,
            "role": "me" if m.direction == "out" else "them",
            "text": m.text or m.caption or f"[{m.media_type or 'media'}]",
            "date_utc": m.date_utc.isoformat() if m.date_utc else None,
            "link": link,
            "user": {
                "id": int(u.id) if u else None,
                "username": getattr(u, "username", None) if u else None,
                "first_name": getattr(u, "first_name", None) if u else None,
            },
        })
    return items, {"count": len(items), "limit": limit}
