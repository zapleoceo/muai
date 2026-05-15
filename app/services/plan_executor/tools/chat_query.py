from __future__ import annotations

from sqlalchemy import case, or_, select, text

from app.db.database import AsyncSessionLocal
from app.db.models import Chat, Message
from app.services.answering_types import PlanChatType, PlanScope
from app.services.plan_executor.time_range import ResolvedRange
from app.services.plan_executor.tools._helpers import _find_chat_by_query, _msg_row


async def tool_sql_recent_messages_by_chat_query(
    *,
    scope: PlanScope,
    chat_id: int,
    chat_query: str,
    chat_types: list[PlanChatType] | None,
    limit: int,
) -> tuple[list[dict], dict]:
    q_norm = str(chat_query or "").strip().strip('"').strip("'").lstrip("@")
    if not q_norm:
        return [], {"count": 0, "error": "empty_chat_query"}
    like = f"%{q_norm}%"

    async with AsyncSessionLocal() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        if scope == PlanScope.CURRENT_CHAT:
            selected_chat = (await session.execute(select(Chat).where(Chat.id == chat_id).limit(1))).scalar_one_or_none()
        else:
            score = (
                case((Chat.username.ilike(q_norm), 100), else_=0)
                + case((Chat.title.ilike(q_norm), 90), else_=0)
                + case((Chat.title.ilike(like), 60), else_=0)
                + case((Chat.username.ilike(like), 40), else_=0)
            ).label("score")
            cq = select(Chat, score).where(or_(Chat.title.ilike(like), Chat.username.ilike(like)))
            if chat_types:
                cq = cq.where(Chat.type.in_([ct.value for ct in chat_types]))
            row = (await session.execute(cq.order_by(score.desc(), Chat.title.asc().nulls_last(), Chat.id.asc()).limit(1))).first()
            selected_chat = row[0] if row else None

        if not selected_chat:
            return [], {"count": 0, "chat_query": q_norm, "error": "chat_not_found"}

        rows = (await session.execute(
            select(Message, Chat)
            .join(Chat, Chat.id == Message.chat_id)
            .where(Message.chat_id == selected_chat.id)
            .order_by(Message.date_utc.desc())
            .limit(limit)
        )).all()

    items = [_msg_row(m, c) for (m, c) in rows]
    return items, {
        "count": len(items),
        "limit": limit,
        "chat_query": q_norm,
        "selected_chat": {"id": int(selected_chat.id), "type": selected_chat.type, "title": selected_chat.title, "username": selected_chat.username, "folder": selected_chat.folder},
    }


async def tool_sql_media_messages_by_chat_query(
    *,
    scope: PlanScope,
    chat_id: int,
    chat_query: str | None,
    chat_types: list[PlanChatType] | None,
    media_type: str,
    limit: int,
    resolved: ResolvedRange | None = None,
) -> tuple[list[dict], dict]:
    q_norm = str(chat_query or "").strip().strip('"').strip("'").lstrip("@")
    media_norm = str(media_type or "").strip().lower()
    if not media_norm:
        return [], {"count": 0, "error": "empty_media_type"}

    async with AsyncSessionLocal() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        if scope == PlanScope.CURRENT_CHAT:
            selected_chat = (await session.execute(select(Chat).where(Chat.id == chat_id).limit(1))).scalar_one_or_none()
        else:
            if q_norm:
                row = (await session.execute(_find_chat_by_query(q_norm, chat_types))).first()
                selected_chat = row[0] if row else None
            else:
                selected_chat = None  # all-chats mode

    if scope != PlanScope.CURRENT_CHAT and not q_norm:
        # all-chats mode: search across all chats
        async with AsyncSessionLocal() as session2:
            await session2.execute(text("SET TRANSACTION READ ONLY"))
            q = (
                select(Message, Chat)
                .join(Chat, Chat.id == Message.chat_id)
                .where(Message.media_type == media_norm)
            )
            if chat_types:
                q = q.where(Chat.type.in_([ct.value for ct in chat_types]))
            if resolved:
                q = q.where(Message.date_utc >= resolved.from_utc, Message.date_utc < resolved.to_utc)
            rows = (await session2.execute(q.order_by(Message.date_utc.desc()).limit(limit))).all()
        items = [_msg_row(m, c) for (m, c) in rows]
        return items, {
            "count": len(items),
            "limit": limit,
            "media_type": media_norm,
            "from_utc": resolved.from_utc.isoformat() if resolved else None,
            "to_utc": resolved.to_utc.isoformat() if resolved else None,
        }

    if not selected_chat:
        return [], {"count": 0, "chat_query": q_norm, "error": "chat_not_found"}

    async with AsyncSessionLocal() as session3:
        await session3.execute(text("SET TRANSACTION READ ONLY"))
        q = (
            select(Message, Chat)
            .join(Chat, Chat.id == Message.chat_id)
            .where(Message.chat_id == selected_chat.id, Message.media_type == media_norm)
        )
        if resolved:
            q = q.where(Message.date_utc >= resolved.from_utc, Message.date_utc < resolved.to_utc)
        rows = (await session3.execute(q.order_by(Message.date_utc.desc()).limit(limit))).all()

    items = [_msg_row(m, c) for (m, c) in rows]
    return items, {
        "count": len(items),
        "limit": limit,
        "media_type": media_norm,
        "chat_query": q_norm or None,
        "selected_chat": {"id": int(selected_chat.id), "type": selected_chat.type, "title": selected_chat.title, "username": selected_chat.username, "folder": selected_chat.folder},
        "from_utc": resolved.from_utc.isoformat() if resolved else None,
        "to_utc": resolved.to_utc.isoformat() if resolved else None,
    }


async def tool_sql_message_by_tg_ref(
    *,
    chat_username: str | None = None,
    chat_id: int | None = None,
    telegram_msg_id: int,
) -> tuple[list[dict], dict]:
    if not telegram_msg_id:
        return [], {"count": 0, "error": "empty_telegram_msg_id"}

    async with AsyncSessionLocal() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        c = None
        if chat_id is not None:
            c = (await session.execute(select(Chat).where(Chat.id == int(chat_id)))).scalar_one_or_none()
        if not c and chat_username:
            u = str(chat_username or "").strip().lstrip("@")
            c = (await session.execute(select(Chat).where(Chat.username.ilike(u)))).scalar_one_or_none()
            if not c:
                c = (await session.execute(select(Chat).where(Chat.username.ilike(f"%{u}%")))).scalar_one_or_none()
        if not c:
            return [], {"count": 0, "error": "chat_not_found", "chat_username": chat_username, "chat_id": chat_id}

        rows = (await session.execute(
            select(Message, Chat)
            .join(Chat, Chat.id == Message.chat_id)
            .where(Message.chat_id == c.id, Message.telegram_msg_id == int(telegram_msg_id))
            .limit(5)
        )).all()

    items = [_msg_row(m, chat) for (m, chat) in rows]
    return items, {"count": len(items), "chat_username": getattr(c, "username", None), "chat_id": int(c.id), "telegram_msg_id": int(telegram_msg_id)}


async def tool_sql_messages_by_chat_query_and_date(
    *,
    scope: PlanScope,
    chat_id: int,
    resolved: ResolvedRange,
    chat_query: str,
    chat_types: list[PlanChatType] | None,
    max_rows: int,
) -> tuple[list[dict], dict]:
    q_norm = str(chat_query or "").strip().lstrip("@")
    if not q_norm:
        return [], {"count": 0, "error": "empty_chat_query"}
    like = f"%{q_norm}%"

    async with AsyncSessionLocal() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        q = (
            select(Message, Chat)
            .join(Chat, Chat.id == Message.chat_id)
            .where(Message.date_utc >= resolved.from_utc, Message.date_utc < resolved.to_utc)
        )
        if scope == PlanScope.CURRENT_CHAT:
            q = q.where(Message.chat_id == chat_id)
        if chat_types:
            q = q.where(Chat.type.in_([ct.value for ct in chat_types]))
        q = q.where(or_(Chat.title.ilike(like), Chat.username.ilike(like), text("('@' || chats.username) ILIKE :like")))
        rows = (await session.execute(q.order_by(Message.date_utc.asc()).limit(max_rows), {"like": like})).all()

    items = [_msg_row(m, c) for (m, c) in rows]
    return items, {"count": len(items), "max_rows": max_rows, "chat_query": q_norm, "from_utc": resolved.from_utc.isoformat(), "to_utc": resolved.to_utc.isoformat()}


async def tool_sql_messages_by_folder_and_date(
    *,
    scope: PlanScope,
    chat_id: int,
    resolved: ResolvedRange,
    folder: str,
    chat_types: list[PlanChatType] | None,
    max_rows: int,
) -> tuple[list[dict], dict]:
    folder_raw = str(folder or "").strip().strip('"').strip("'")
    if not folder_raw:
        return [], {"count": 0, "error": "empty_folder"}
    like = f"%{folder_raw}%"

    async with AsyncSessionLocal() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        q = (
            select(Message, Chat)
            .join(Chat, Chat.id == Message.chat_id)
            .where(Message.date_utc >= resolved.from_utc, Message.date_utc < resolved.to_utc)
            .where(Chat.folder.ilike(like))
        )
        if scope == PlanScope.CURRENT_CHAT:
            q = q.where(Message.chat_id == chat_id)
        if chat_types:
            q = q.where(Chat.type.in_([ct.value for ct in chat_types]))
        rows = (await session.execute(q.order_by(Message.date_utc.asc()).limit(max_rows))).all()

    items = [_msg_row(m, c) for (m, c) in rows]
    return items, {"count": len(items), "max_rows": max_rows, "folder": folder_raw, "from_utc": resolved.from_utc.isoformat(), "to_utc": resolved.to_utc.isoformat()}
