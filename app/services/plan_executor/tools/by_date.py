from __future__ import annotations

from sqlalchemy import func, select, text

from app.db.database import AsyncSessionLocal
from app.db.models import Chat, Message
from app.services.answering_types import PlanChatType, PlanScope
from app.services.plan_executor.links import build_message_link
from app.services.plan_executor.time_range import ResolvedRange
from app.services.plan_executor.tools._helpers import _msg_row


async def tool_sql_messages_by_date(
    *,
    chat_id: int,
    scope: PlanScope,
    chat_types: list[PlanChatType] | None,
    chat_ids: list[int] | None,
    resolved: ResolvedRange,
    max_rows: int,
) -> tuple[list[dict], dict]:
    async with AsyncSessionLocal() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        q = select(Message)
        if chat_types:
            q = q.join(Chat, Chat.id == Message.chat_id).where(Chat.type.in_([ct.value for ct in chat_types]))
        q = q.where(Message.date_utc >= resolved.from_utc, Message.date_utc < resolved.to_utc)
        if scope == PlanScope.CURRENT_CHAT:
            q = q.where(Message.chat_id == chat_id)
        elif chat_ids:
            q = q.where(Message.chat_id.in_(chat_ids))
        q = q.order_by(Message.date_utc.asc()).limit(max_rows)
        rows = list((await session.execute(q)).scalars().all())
        chat_map: dict[int, Chat] = {}
        ids = sorted({int(m.chat_id) for m in rows})
        if ids:
            chats = list((await session.execute(select(Chat).where(Chat.id.in_(ids)))).scalars().all())
            chat_map = {int(c.id): c for c in chats}

    items = [
        {
            "chat_id": int(m.chat_id),
            "chat": {
                "id": int(m.chat_id),
                "type": getattr(chat_map.get(int(m.chat_id)), "type", None),
                "title": getattr(chat_map.get(int(m.chat_id)), "title", None),
                "username": getattr(chat_map.get(int(m.chat_id)), "username", None),
            },
            "message_id": int(m.id),
            "telegram_msg_id": int(m.telegram_msg_id) if m.telegram_msg_id is not None else None,
            "direction": m.direction,
            "role": "assistant" if m.direction == "out" else "user",
            "text": m.text or m.caption or f"[{m.media_type or 'media'}]",
            "date_utc": m.date_utc.isoformat() if m.date_utc else None,
            "dialog_key": m.dialog_key,
            "link": build_message_link(
                chat_id=int(m.chat_id),
                chat_type=getattr(chat_map.get(int(m.chat_id)), "type", None),
                chat_username=getattr(chat_map.get(int(m.chat_id)), "username", None),
                telegram_msg_id=int(m.telegram_msg_id) if m.telegram_msg_id is not None else None,
            ),
        }
        for m in rows
    ]
    return items, {
        "count": len(items),
        "max_rows": max_rows,
        "from_utc": resolved.from_utc.isoformat(),
        "to_utc": resolved.to_utc.isoformat(),
    }


async def tool_sql_stats_by_date(
    *,
    chat_id: int,
    scope: PlanScope,
    chat_types: list[PlanChatType] | None,
    chat_ids: list[int] | None,
    resolved: ResolvedRange,
) -> tuple[dict, dict]:
    async with AsyncSessionLocal() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        q = select(func.count()).select_from(Message)
        if chat_types:
            q = q.join(Chat, Chat.id == Message.chat_id).where(Chat.type.in_([ct.value for ct in chat_types]))
        q = q.where(Message.date_utc >= resolved.from_utc, Message.date_utc < resolved.to_utc)
        if scope == PlanScope.CURRENT_CHAT:
            q = q.where(Message.chat_id == chat_id)
        elif chat_ids:
            q = q.where(Message.chat_id.in_(chat_ids))
        total = (await session.execute(q)).scalar() or 0
    return {"messages": int(total)}, {"from_utc": resolved.from_utc.isoformat(), "to_utc": resolved.to_utc.isoformat()}


async def tool_sql_active_chats_by_date(
    *,
    chat_id: int,
    scope: PlanScope,
    chat_types: list[PlanChatType] | None,
    chat_ids: list[int] | None,
    resolved: ResolvedRange,
    limit: int = 50,
) -> tuple[list[dict], dict]:
    async with AsyncSessionLocal() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        q = (
            select(
                Chat.id.label("chat_id"),
                Chat.type.label("chat_type"),
                Chat.title.label("chat_title"),
                Chat.username.label("chat_username"),
                func.count(Message.id).label("message_count"),
                func.max(Message.date_utc).label("last_date_utc"),
            )
            .join(Message, Message.chat_id == Chat.id)
            .where(Message.date_utc >= resolved.from_utc, Message.date_utc < resolved.to_utc)
        )
        if chat_types:
            q = q.where(Chat.type.in_([ct.value for ct in chat_types]))
        if scope == PlanScope.CURRENT_CHAT:
            q = q.where(Chat.id == chat_id)
        elif chat_ids:
            q = q.where(Chat.id.in_(chat_ids))
        q = q.group_by(Chat.id, Chat.type, Chat.title, Chat.username).order_by(func.count(Message.id).desc()).limit(limit)
        rows = (await session.execute(q)).all()

    items = [
        {
            "chat_id": int(r.chat_id),
            "type": str(r.chat_type),
            "title": r.chat_title,
            "username": r.chat_username,
            "message_count": int(r.message_count or 0),
            "last_date_utc": r.last_date_utc.isoformat() if r.last_date_utc else None,
        }
        for r in rows
    ]
    return items, {
        "count": len(items),
        "limit": int(limit),
        "from_utc": resolved.from_utc.isoformat(),
        "to_utc": resolved.to_utc.isoformat(),
    }


async def tool_sql_search_messages_by_date(
    *,
    chat_id: int,
    scope: PlanScope,
    chat_types: list[PlanChatType] | None,
    chat_ids: list[int] | None,
    resolved: ResolvedRange,
    query: str,
    limit: int,
) -> tuple[list[dict], dict]:
    q_raw = str(query or "").strip()
    if not q_raw:
        return [], {"count": 0, "error": "empty_query"}
    q_like = f"%{q_raw}%"
    from sqlalchemy import or_
    async with AsyncSessionLocal() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        q = (
            select(Message, Chat)
            .join(Chat, Chat.id == Message.chat_id)
            .where(Message.date_utc >= resolved.from_utc, Message.date_utc < resolved.to_utc)
            .where(or_(Message.text.ilike(q_like), Message.caption.ilike(q_like)))
        )
        if chat_types:
            q = q.where(Chat.type.in_([ct.value for ct in chat_types]))
        if scope == PlanScope.CURRENT_CHAT:
            q = q.where(Message.chat_id == chat_id)
        elif chat_ids:
            q = q.where(Message.chat_id.in_(chat_ids))
        q = q.order_by(Message.date_utc.desc()).limit(limit)
        rows = (await session.execute(q)).all()
    items = [_msg_row(m, c) for (m, c) in rows]
    return items, {"count": len(items), "limit": limit, "query": q_raw, "from_utc": resolved.from_utc.isoformat(), "to_utc": resolved.to_utc.isoformat()}
