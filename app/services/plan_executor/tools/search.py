from __future__ import annotations

from sqlalchemy import case, or_, select, text

from app.db.database import AsyncSessionLocal
from app.db.models import Chat, Message
from app.services.answering_types import PlanChatType, PlanScope
from app.services.plan_executor.links import build_message_link
from app.services.plan_executor.time_range import ResolvedRange
from app.services.plan_executor.tools._helpers import (
    _find_chat_by_query,
    _has_cyrillic,
    _has_latin,
    _msg_row,
)


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


async def tool_sql_lex_search_messages(
    *,
    chat_id: int,
    scope: PlanScope,
    chat_types: list[PlanChatType] | None,
    chat_ids: list[int] | None,
    chat_query: str | None = None,
    query: str,
    limit: int,
    resolved: ResolvedRange | None = None,
) -> tuple[list[dict], dict]:
    q_raw = str(query or "").strip()
    if not q_raw:
        return [], {"count": 0, "error": "empty_query"}

    # Resolve chat_query string to numeric chat_id (overrides chat_ids)
    resolved_chat_ids = list(chat_ids or [])
    selected_chat_title: str | None = None
    if chat_query and scope != PlanScope.CURRENT_CHAT:
        q_norm = str(chat_query).strip().strip('"').strip("'").lstrip("@")
        async with AsyncSessionLocal() as _s:
            await _s.execute(text("SET TRANSACTION READ ONLY"))
            row = (await _s.execute(_find_chat_by_query(q_norm, chat_types))).first()
        if not row:
            return [], {"count": 0, "chat_query": q_norm, "error": "chat_not_found"}
        resolved_chat_ids = [int(row[0].id)]
        selected_chat_title = row[0].title

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
    elif resolved_chat_ids:
        where.append("m.chat_id = ANY(CAST(:chat_ids AS bigint[]))")
        params["chat_ids"] = [int(x) for x in resolved_chat_ids]

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
            OR (({q_expr}) % :q)
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
    meta: dict = {"count": len(items), "limit": int(limit), "query": q_raw, "use_ru": bool(use_ru), "use_en": bool(use_en)}
    if selected_chat_title:
        meta["selected_chat"] = selected_chat_title
    return items, meta


async def tool_sql_chats_by_topic(
    *,
    query: str,
    chat_types: list[PlanChatType] | None,
    limit: int = 100,
    resolved: ResolvedRange | None = None,
) -> tuple[list[dict], dict]:
    """FTS search grouped by chat — returns unique chats with hit counts, not individual messages."""
    q_raw = str(query or "").strip()
    if not q_raw:
        return [], {"count": 0, "error": "empty_query"}

    use_ru = _has_cyrillic(q_raw)
    use_en = _has_latin(q_raw)
    q_expr = "coalesce(m.text,'') || ' ' || coalesce(m.caption,'')"

    params: dict[str, object] = {"q": q_raw, "lim": int(limit), "use_ru": bool(use_ru), "use_en": bool(use_en)}
    where: list[str] = []
    if chat_types:
        where.append("c.type = ANY(CAST(:chat_types AS text[]))")
        params["chat_types"] = [ct.value for ct in chat_types]
    if resolved is not None:
        where.append("m.date_utc >= :from_utc AND m.date_utc < :to_utc")
        params["from_utc"] = resolved.from_utc
        params["to_utc"] = resolved.to_utc

    where_sql = ("WHERE " + " AND ".join(where) + " AND") if where else "WHERE"

    sql = text(f"""
        WITH q AS (
            SELECT
                websearch_to_tsquery('simple', :q)  AS qs,
                websearch_to_tsquery('russian', :q) AS qru,
                websearch_to_tsquery('english', :q) AS qen
        )
        SELECT
            c.id          AS chat_id,
            c.type        AS chat_type,
            c.title       AS chat_title,
            c.username    AS chat_username,
            c.folder      AS folder,
            COUNT(m.id)   AS hit_count,
            MAX(m.date_utc) AS last_hit_utc
        FROM messages m
        JOIN chats c ON c.id = m.chat_id
        CROSS JOIN q
        {where_sql}
          (
            to_tsvector('simple', {q_expr}) @@ q.qs
            OR (CASE WHEN :use_ru THEN to_tsvector('russian', {q_expr}) @@ q.qru ELSE false END)
            OR (CASE WHEN :use_en THEN to_tsvector('english', {q_expr}) @@ q.qen ELSE false END)
            OR (({q_expr}) % :q)
          )
        GROUP BY c.id, c.type, c.title, c.username, c.folder
        ORDER BY hit_count DESC, c.title ASC NULLS LAST
        LIMIT :lim
    """)

    async with AsyncSessionLocal() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        await session.execute(text("SELECT set_limit(0.12)"))
        rows = (await session.execute(sql, params)).fetchall()

    items = [
        {
            "chat_id": int(r.chat_id),
            "type": r.chat_type,
            "title": r.chat_title,
            "username": r.chat_username,
            "folder": r.folder,
            "hit_count": int(r.hit_count),
            "last_hit_utc": r.last_hit_utc.isoformat() if r.last_hit_utc else None,
        }
        for r in rows
    ]
    return items, {
        "count": len(items),
        "limit": int(limit),
        "query": q_raw,
        "from_utc": resolved.from_utc.isoformat() if resolved else None,
        "to_utc": resolved.to_utc.isoformat() if resolved else None,
    }
