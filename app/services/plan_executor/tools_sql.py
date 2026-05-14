from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import case, func, select, text, or_

from app.db.database import AsyncSessionLocal
from app.db.models import Chat, Message
from app.db.repository import MessageRepo
from app.services.answering_types import (
    DynamicFilterOp,
    DynamicSelectAgg,
    DynamicToolSpec,
    PlanChatType,
    PlanScope,
)
from app.services.plan_executor.links import build_message_link
from app.services.plan_executor.time_range import ResolvedRange


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


def _msg_row(m: Message, c: Chat) -> dict:
    return {
        "chat_id": int(m.chat_id),
        "chat": {"id": int(m.chat_id), "type": c.type, "title": c.title, "username": c.username, "folder": getattr(c, "folder", None)},
        "message_id": int(m.id),
        "telegram_msg_id": int(m.telegram_msg_id) if m.telegram_msg_id is not None else None,
        "direction": m.direction,
        "role": "me" if m.direction == "out" else "them",
        "text": m.text or m.caption or f"[{m.media_type or 'media'}]",
        "date_utc": m.date_utc.isoformat() if m.date_utc else None,
        "link": build_message_link(
            chat_id=int(m.chat_id),
            chat_type=c.type,
            chat_username=c.username,
            telegram_msg_id=int(m.telegram_msg_id) if m.telegram_msg_id is not None else None,
        ),
    }


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


async def tool_sql_dynamic_query(
    *,
    chat_id: int,
    scope: PlanScope,
    chat_types: list[PlanChatType] | None,
    chat_ids: list[int] | None,
    resolved: ResolvedRange | None,
    spec: DynamicToolSpec,
) -> tuple[list[dict], dict]:
    def _field_expr(name: str):
        m = {
            "message_id": Message.id,
            "chat_id": Message.chat_id,
            "user_id": Message.user_id,
            "telegram_msg_id": Message.telegram_msg_id,
            "direction": Message.direction,
            "media_type": Message.media_type,
            "date_utc": Message.date_utc,
            "text": Message.text,
            "caption": Message.caption,
            "chat_type": Chat.type,
            "chat_title": Chat.title,
            "chat_username": Chat.username,
            "folder": Chat.folder,
        }
        if name == "text_any":
            return func.coalesce(Message.text, "") + " " + func.coalesce(Message.caption, "")
        return m.get(name)

    if spec.require_time_range and resolved is None:
        return [], {"count": 0, "error": "time_range_required"}

    async with AsyncSessionLocal() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))

        cols = []
        keys = []
        for s in spec.select:
            expr = _field_expr(s.field)
            if expr is None:
                raise ValueError(f"unsupported_field:{s.field}")
            if s.agg:
                if s.agg == DynamicSelectAgg.COUNT:
                    expr = func.count(expr)
                elif s.agg == DynamicSelectAgg.COUNT_DISTINCT:
                    expr = func.count(expr.distinct())
                elif s.agg == DynamicSelectAgg.MAX:
                    expr = func.max(expr)
                elif s.agg == DynamicSelectAgg.MIN:
                    expr = func.min(expr)
                else:
                    raise ValueError(f"unsupported_agg:{s.agg}")
            key = s.as_name or (f"{s.agg.value.lower()}_{s.field}" if s.agg else s.field)
            cols.append(expr.label(key))
            keys.append(key)

        q = select(*cols).select_from(Message).join(Chat, Chat.id == Message.chat_id)

        where = []
        if resolved is not None:
            where.append(Message.date_utc >= resolved.from_utc)
            where.append(Message.date_utc < resolved.to_utc)
        if chat_types:
            where.append(Chat.type.in_([ct.value for ct in chat_types]))
        if scope == PlanScope.CURRENT_CHAT:
            where.append(Message.chat_id == chat_id)
        elif chat_ids:
            where.append(Message.chat_id.in_(chat_ids))

        for f in spec.filters:
            expr = _field_expr(f.field)
            if expr is None:
                raise ValueError(f"unsupported_field:{f.field}")
            if f.op == DynamicFilterOp.EQ:
                where.append(expr == f.value)
            elif f.op == DynamicFilterOp.ILIKE:
                v = str(f.value or "").strip()
                where.append(expr.ilike(f"%{v}%"))
            elif f.op == DynamicFilterOp.IN:
                if not isinstance(f.value, list):
                    raise ValueError("IN requires list value")
                where.append(expr.in_(f.value))
            elif f.op == DynamicFilterOp.BETWEEN:
                if f.value is None or f.value_to is None:
                    raise ValueError("BETWEEN requires value and value_to")
                where.append(expr >= f.value)
                where.append(expr <= f.value_to)
            elif f.op == DynamicFilterOp.IS_NOT_NULL:
                where.append(expr.isnot(None))
            else:
                raise ValueError(f"unsupported_op:{f.op}")

        if where:
            q = q.where(*where)
        if spec.group_by:
            group_exprs = [_field_expr(g) for g in spec.group_by]
            if any(e is None for e in group_exprs):
                raise ValueError("unsupported field in group_by")
            q = q.group_by(*group_exprs)
        if spec.order_by:
            order_exprs = []
            for o in spec.order_by:
                expr = _field_expr(o.field)
                if expr is None:
                    raise ValueError(f"unsupported_field:{o.field}")
                order_exprs.append(expr.desc() if o.desc else expr.asc())
            q = q.order_by(*order_exprs)

        q = q.limit(int(spec.limit))
        rows = (await session.execute(q)).all()

    items = []
    for r in rows:
        d = dict(r._mapping) if hasattr(r, "_mapping") else {k: v for k, v in zip(keys, r, strict=False)}
        for key in ("date_utc", "last_date_utc"):
            if key in d and isinstance(d[key], datetime):
                d[key] = d[key].isoformat()
        if d.get("telegram_msg_id") and d.get("chat_id"):
            link = build_message_link(
                chat_id=int(d.get("chat_id") or 0),
                chat_type=d.get("chat_type"),
                chat_username=d.get("chat_username"),
                telegram_msg_id=int(d.get("telegram_msg_id") or 0),
            )
            if link:
                d["link"] = link
        items.append(d)

    return items, {
        "count": len(items),
        "limit": int(spec.limit),
        "group_by": spec.group_by,
        "from_utc": resolved.from_utc.isoformat() if resolved else None,
        "to_utc": resolved.to_utc.isoformat() if resolved else None,
    }


async def tool_sql_search_messages(
    *,
    chat_id: int,
    scope: PlanScope,
    chat_types: list[PlanChatType] | None,
    chat_ids: list[int] | None,
    query: str,
    limit: int,
) -> tuple[list[dict], dict]:
    q_like = f"%{query}%"
    async with AsyncSessionLocal() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        q = (
            select(Message, Chat)
            .join(Chat, Chat.id == Message.chat_id)
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
    return items, {"count": len(items), "limit": limit}


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


def _find_chat_by_query(q_norm: str, chat_types: list[PlanChatType] | None):
    like = f"%{q_norm}%"
    score = (
        case((Chat.username.ilike(q_norm), 100), else_=0)
        + case((Chat.title.ilike(q_norm), 90), else_=0)
        + case((Chat.title.ilike(like), 60), else_=0)
        + case((Chat.username.ilike(like), 40), else_=0)
    ).label("score")
    cq = select(Chat, score).where(or_(Chat.title.ilike(like), Chat.username.ilike(like)))
    if chat_types:
        cq = cq.where(Chat.type.in_([ct.value for ct in chat_types]))
    return cq.order_by(score.desc(), Chat.title.asc().nulls_last(), Chat.id.asc()).limit(1)


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
            if not q_norm:
                return [], {"count": 0, "error": "empty_chat_query"}
            row = (await session.execute(_find_chat_by_query(q_norm, chat_types))).first()
            selected_chat = row[0] if row else None

        if not selected_chat:
            return [], {"count": 0, "chat_query": q_norm, "error": "chat_not_found"}

        q = (
            select(Message, Chat)
            .join(Chat, Chat.id == Message.chat_id)
            .where(Message.chat_id == selected_chat.id, Message.media_type == media_norm)
        )
        if resolved:
            q = q.where(Message.date_utc >= resolved.from_utc, Message.date_utc < resolved.to_utc)
        rows = (await session.execute(q.order_by(Message.date_utc.desc()).limit(limit))).all()

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


async def tool_sql_find_chats(
    *,
    query: str,
    limit: int,
    chat_types: list[PlanChatType] | None,
) -> tuple[list[dict], dict]:
    q_raw = str(query or "").strip().strip('"').strip("'")
    if not q_raw:
        return [], {"count": 0, "error": "empty_query"}
    like = f"%{q_raw.lstrip('@')}%"

    async with AsyncSessionLocal() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        score = (
            case((Chat.username.ilike(q_raw.lstrip("@")), 100), else_=0)
            + case((Chat.title.ilike(q_raw), 90), else_=0)
            + case((Chat.title.ilike(like), 60), else_=0)
            + case((Chat.folder.ilike(like), 50), else_=0)
            + case((Chat.username.ilike(like), 40), else_=0)
        ).label("score")
        q = select(Chat, score).where(or_(Chat.title.ilike(like), Chat.username.ilike(like), Chat.folder.ilike(like)))
        if chat_types:
            q = q.where(Chat.type.in_([ct.value for ct in chat_types]))
        q = q.order_by(score.desc(), Chat.title.asc().nulls_last(), Chat.id.asc()).limit(limit)
        rows = (await session.execute(q)).all()

    items = [{"chat_id": int(c.id), "type": c.type, "title": c.title, "username": c.username, "folder": c.folder, "score": int(s or 0)} for (c, s) in rows]
    return items, {"count": len(items), "limit": limit, "query": q_raw}


def _has_cyrillic(s: str) -> bool:
    return bool(re.search(r"[Ѐ-ӿ]", s))


def _has_latin(s: str) -> bool:
    return bool(re.search(r"[A-Za-z]", s))


async def tool_sql_lex_search_messages(
    *,
    chat_id: int,
    scope: PlanScope,
    chat_types: list[PlanChatType] | None,
    chat_ids: list[int] | None,
    query: str,
    limit: int,
    resolved: ResolvedRange | None = None,
) -> tuple[list[dict], dict]:
    q_raw = str(query or "").strip()
    if not q_raw:
        return [], {"count": 0, "error": "empty_query"}

    use_ru = _has_cyrillic(q_raw)
    use_en = _has_latin(q_raw)
    # q_expr is a safe constant used only in the SQL template — no user input interpolated
    q_expr = "coalesce(m.text,'') || ' ' || coalesce(m.caption,'')"

    params: dict[str, object] = {"q": q_raw, "lim": int(limit), "use_ru": bool(use_ru), "use_en": bool(use_en)}
    where = []
    if resolved is not None:
        where.append("m.date_utc >= :from_utc AND m.date_utc < :to_utc")
        params["from_utc"] = resolved.from_utc
        params["to_utc"] = resolved.to_utc
    if chat_types:
        where.append("c.type = ANY(CAST(:chat_types AS text[]))")
        params["chat_types"] = [ct.value for ct in chat_types]
    if scope == PlanScope.CURRENT_CHAT:
        where.append("m.chat_id = :chat_id")
        params["chat_id"] = int(chat_id)
    elif chat_ids:
        where.append("m.chat_id = ANY(CAST(:chat_ids AS bigint[]))")
        params["chat_ids"] = [int(x) for x in chat_ids]

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    sql = text(f"""
        WITH q AS (
          SELECT
            websearch_to_tsquery('simple', :q)  AS qs,
            websearch_to_tsquery('russian', :q) AS qru,
            websearch_to_tsquery('english', :q) AS qen
        )
        SELECT
          m.id AS message_id, m.chat_id, m.telegram_msg_id, m.direction,
          m.text, m.caption, m.media_type, m.date_utc,
          c.type AS chat_type, c.title AS chat_title, c.username AS chat_username,
          ts_rank_cd(to_tsvector('simple', {q_expr}), q.qs) AS r_simple,
          CASE WHEN :use_ru THEN ts_rank_cd(to_tsvector('russian', {q_expr}), q.qru) ELSE 0 END AS r_ru,
          CASE WHEN :use_en THEN ts_rank_cd(to_tsvector('english', {q_expr}), q.qen) ELSE 0 END AS r_en,
          similarity({q_expr}, :q) AS sim
        FROM messages m
        JOIN chats c ON c.id = m.chat_id
        CROSS JOIN q
        {where_sql}
          AND (
            to_tsvector('simple', {q_expr}) @@ q.qs
            OR ({q_expr} % :q)
          )
        ORDER BY (r_simple + r_ru + r_en + sim) DESC, m.date_utc DESC, m.id DESC
        LIMIT :lim
    """)

    async with AsyncSessionLocal() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        await session.execute(text("SELECT set_limit(0.12)"))
        rows = (await session.execute(sql, params)).fetchall()

    items = [
        {
            "chat_id": int(r.chat_id),
            "chat": {"id": int(r.chat_id), "type": r.chat_type, "title": r.chat_title, "username": r.chat_username},
            "message_id": int(r.message_id),
            "telegram_msg_id": int(r.telegram_msg_id) if r.telegram_msg_id is not None else None,
            "direction": r.direction,
            "role": "me" if r.direction == "out" else "them",
            "text": r.text or r.caption or f"[{r.media_type or 'media'}]",
            "date_utc": r.date_utc.isoformat() if r.date_utc else None,
            "link": build_message_link(
                chat_id=int(r.chat_id),
                chat_type=r.chat_type,
                chat_username=r.chat_username,
                telegram_msg_id=int(r.telegram_msg_id) if r.telegram_msg_id is not None else None,
            ),
            "score": float((r.r_simple or 0) + (r.r_ru or 0) + (r.r_en or 0) + (r.sim or 0)),
        }
        for r in rows
    ]
    return items, {"count": len(items), "limit": int(limit), "query": q_raw, "use_ru": bool(use_ru), "use_en": bool(use_en)}


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
