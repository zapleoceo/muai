from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text, or_

from app.db.database import AsyncSessionLocal
from app.db.models import Chat, Message
from app.db.repository import MessageRepo
from app.llm.embedding import embed_text
from app.services.answering_types import Plan, PlanChatType, PlanScope, PlanTimeRange, RetrievedContext, ToolRun


@dataclass(frozen=True)
class ResolvedRange:
    from_utc: datetime
    to_utc: datetime


def build_message_link(*, chat_id: int, chat_type: str | None, chat_username: str | None, telegram_msg_id: int | None) -> str | None:
    if not telegram_msg_id:
        return None
    if chat_username:
        u = chat_username.lstrip("@")
        return f"https://t.me/{u}/{telegram_msg_id}"
    s = str(chat_id)
    if s.startswith("-100"):
        internal = s[4:]
        return f"https://t.me/c/{internal}/{telegram_msg_id}"
    if chat_type in ("group", "supergroup", "channel") and chat_id < 0:
        internal = str(abs(chat_id))
        return f"https://t.me/c/{internal}/{telegram_msg_id}"
    return None


def _parse_explicit(v: str) -> datetime | date:
    s = v.strip()
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return date.fromisoformat(s)


def resolve_time_range(*, time_range: PlanTimeRange, tz: str, explicit_from: str | None, explicit_to: str | None) -> ResolvedRange | None:
    if time_range == PlanTimeRange.NONE:
        return None

    zone = ZoneInfo(tz)
    now_local = datetime.now(tz=zone)
    today_local = now_local.date()

    if time_range == PlanTimeRange.TODAY:
        start_local = datetime.combine(today_local, time(0, 0), tzinfo=zone)
        end_local = start_local + timedelta(days=1)
    elif time_range == PlanTimeRange.YESTERDAY:
        start_local = datetime.combine(today_local - timedelta(days=1), time(0, 0), tzinfo=zone)
        end_local = datetime.combine(today_local, time(0, 0), tzinfo=zone)
    elif time_range == PlanTimeRange.LAST_7_DAYS:
        start_local = datetime.combine(today_local - timedelta(days=6), time(0, 0), tzinfo=zone)
        end_local = datetime.combine(today_local + timedelta(days=1), time(0, 0), tzinfo=zone)
    elif time_range == PlanTimeRange.EXPLICIT:
        if not explicit_from or not explicit_to:
            raise ValueError("explicit_from/explicit_to required")
        a = _parse_explicit(explicit_from)
        b = _parse_explicit(explicit_to)
        if isinstance(a, date) and not isinstance(a, datetime):
            start_local = datetime.combine(a, time(0, 0), tzinfo=zone)
        else:
            start_local = a if isinstance(a, datetime) else datetime.combine(a, time(0, 0), tzinfo=zone)
            if start_local.tzinfo is None:
                start_local = start_local.replace(tzinfo=zone)
        if isinstance(b, date) and not isinstance(b, datetime):
            end_local = datetime.combine(b + timedelta(days=1), time(0, 0), tzinfo=zone)
        else:
            end_local = b if isinstance(b, datetime) else datetime.combine(b, time(0, 0), tzinfo=zone)
            if end_local.tzinfo is None:
                end_local = end_local.replace(tzinfo=zone)
    else:
        raise ValueError("Unsupported time_range")

    return ResolvedRange(from_utc=start_local.astimezone(timezone.utc), to_utc=end_local.astimezone(timezone.utc))


async def tool_get_recent_dialog(*, chat_id: int, limit: int) -> tuple[list[dict], dict]:
    async with AsyncSessionLocal() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        chat = (await session.execute(select(Chat).where(Chat.id == chat_id))).scalar_one_or_none()
        rows = await MessageRepo(session).get_recent_messages_with_users(chat_id=chat_id, limit=limit)
    items = []
    for (m, u) in rows:
        chat_username = getattr(chat, "username", None) if chat else None
        chat_type = getattr(chat, "type", None) if chat else None
        link = build_message_link(chat_id=int(m.chat_id), chat_type=chat_type, chat_username=chat_username, telegram_msg_id=int(m.telegram_msg_id) if m.telegram_msg_id is not None else None)
        items.append(
            {
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
                "role": "assistant" if m.direction == "out" else "user",
                "text": m.text or m.caption or f"[{m.media_type or 'media'}]",
                "date_utc": m.date_utc.isoformat() if m.date_utc else None,
                "link": link,
                "user": {
                    "id": int(u.id) if u else None,
                    "username": getattr(u, "username", None) if u else None,
                    "first_name": getattr(u, "first_name", None) if u else None,
                },
            }
        )
    return items, {"count": len(items), "limit": limit}


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
        chat_map = {}
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


async def tool_rag_search(
    *,
    chat_id: int,
    scope: PlanScope,
    chat_ids: list[int] | None,
    query: str,
    top_k: int,
) -> tuple[list[dict], dict]:
    q_vec = await embed_text(query, task_type="RETRIEVAL_QUERY")
    async with AsyncSessionLocal() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        rows = await MessageRepo(session).search_chunks(
            q_vec,
            limit=top_k,
            chat_id=chat_id if scope == PlanScope.CURRENT_CHAT else None,
            chat_ids=chat_ids if scope != PlanScope.CURRENT_CHAT else None,
        )
    items = [
        {
            "chunk_id": int(r.id),
            "chat_id": int(r.chat_id),
            "chat_title": r.chat_title,
            "text": r.chunk_text,
            "msg_date_from": r.msg_date_from.isoformat() if getattr(r, "msg_date_from", None) else None,
            "msg_date_to": r.msg_date_to.isoformat() if getattr(r, "msg_date_to", None) else None,
            "chat_username": getattr(r, "chat_username", None),
            "max_tg_msg_id": int(getattr(r, "max_tg_msg_id", 0) or 0) or None,
            "link": build_message_link(
                chat_id=int(r.chat_id),
                chat_type=None,
                chat_username=getattr(r, "chat_username", None),
                telegram_msg_id=int(getattr(r, "max_tg_msg_id", 0) or 0) or None,
            ),
        }
        for r in rows
    ]
    return items, {"count": len(items), "top_k": top_k}


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

    items = []
    for (m, c) in rows:
        items.append(
            {
                "chat_id": int(m.chat_id),
                "chat": {
                    "id": int(m.chat_id),
                    "type": c.type,
                    "title": c.title,
                    "username": c.username,
                },
                "message_id": int(m.id),
                "telegram_msg_id": int(m.telegram_msg_id) if m.telegram_msg_id is not None else None,
                "direction": m.direction,
                "role": "assistant" if m.direction == "out" else "user",
                "text": m.text or m.caption or f"[{m.media_type or 'media'}]",
                "date_utc": m.date_utc.isoformat() if m.date_utc else None,
                "link": build_message_link(
                    chat_id=int(m.chat_id),
                    chat_type=c.type,
                    chat_username=c.username,
                    telegram_msg_id=int(m.telegram_msg_id) if m.telegram_msg_id is not None else None,
                ),
            }
        )
    return items, {"count": len(items), "limit": limit}


async def tool_sql_messages_by_chat_query_and_date(
    *,
    scope: PlanScope,
    chat_id: int,
    resolved: ResolvedRange,
    chat_query: str,
    chat_types: list[PlanChatType] | None,
    max_rows: int,
) -> tuple[list[dict], dict]:
    q_raw = str(chat_query or "").strip()
    q_norm = q_raw.lstrip("@")
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

    items = []
    for (m, c) in rows:
        items.append(
            {
                "chat_id": int(m.chat_id),
                "chat": {"id": int(m.chat_id), "type": c.type, "title": c.title, "username": c.username},
                "message_id": int(m.id),
                "telegram_msg_id": int(m.telegram_msg_id) if m.telegram_msg_id is not None else None,
                "direction": m.direction,
                "role": "assistant" if m.direction == "out" else "user",
                "text": m.text or m.caption or f"[{m.media_type or 'media'}]",
                "date_utc": m.date_utc.isoformat() if m.date_utc else None,
                "link": build_message_link(
                    chat_id=int(m.chat_id),
                    chat_type=c.type,
                    chat_username=c.username,
                    telegram_msg_id=int(m.telegram_msg_id) if m.telegram_msg_id is not None else None,
                ),
            }
        )
    return items, {
        "count": len(items),
        "max_rows": max_rows,
        "chat_query": q_norm,
        "from_utc": resolved.from_utc.isoformat(),
        "to_utc": resolved.to_utc.isoformat(),
    }


_ALLOWED_TOOLS: dict[str, set[str]] = {
    "INFO_ONLY": {"get_recent_dialog"},
    "RAG_SEMANTIC": {"get_recent_dialog", "rag_search", "sql_search_messages"},
    "SQL_DATE_SUMMARY": {"get_recent_dialog", "sql_messages_by_date", "sql_messages_by_chat_query_and_date", "sql_stats_by_date", "sql_search_messages"},
    "HYBRID": {"get_recent_dialog", "rag_search", "sql_messages_by_date", "sql_messages_by_chat_query_and_date", "sql_stats_by_date", "sql_search_messages"},
    "COMMAND": set(),
}


async def execute_plan(
    *,
    plan: Plan,
    chat_id: int,
    query: str,
    timezone_name: str = "UTC",
) -> RetrievedContext:
    ctx = RetrievedContext()
    resolved = resolve_time_range(
        time_range=plan.time_range,
        tz=timezone_name,
        explicit_from=plan.explicit_from,
        explicit_to=plan.explicit_to,
    )
    if resolved:
        ctx.meta["time_range"] = {"from_utc": resolved.from_utc.isoformat(), "to_utc": resolved.to_utc.isoformat()}

    allowed = _ALLOWED_TOOLS.get(plan.strategy.value, set())
    for tc in plan.tools:
        name = tc.name
        if name not in allowed:
            ctx.tool_runs.append(ToolRun(name=name, ok=False, meta={"error": "tool_not_allowed"}))
            continue

        try:
            if name == "get_recent_dialog":
                limit = int(tc.args.get("limit", 20))
                msgs, meta = await tool_get_recent_dialog(chat_id=chat_id, limit=limit)
                ctx.messages.extend(msgs)
                ctx.tool_runs.append(ToolRun(name=name, ok=True, meta=meta))
                continue

            if name == "sql_messages_by_date":
                if not resolved:
                    raise ValueError("time_range required")
                scope = PlanScope(tc.args.get("scope", plan.scope.value))
                chat_types = tc.args.get("chat_types", plan.chat_types)
                if chat_types:
                    chat_types = [PlanChatType(x) for x in chat_types]
                chat_ids = tc.args.get("chat_ids", plan.chat_ids)
                if chat_ids:
                    chat_ids = [int(x) for x in chat_ids]
                max_rows = int(tc.args.get("max_rows", 1500))
                msgs, meta = await tool_sql_messages_by_date(
                    chat_id=chat_id,
                    scope=scope,
                    chat_types=chat_types,
                    chat_ids=chat_ids,
                    resolved=resolved,
                    max_rows=max_rows,
                )
                ctx.messages.extend(msgs)
                ctx.tool_runs.append(ToolRun(name=name, ok=True, meta=meta))
                continue

            if name == "sql_stats_by_date":
                if not resolved:
                    raise ValueError("time_range required")
                scope = PlanScope(tc.args.get("scope", plan.scope.value))
                chat_types = tc.args.get("chat_types", plan.chat_types)
                if chat_types:
                    chat_types = [PlanChatType(x) for x in chat_types]
                chat_ids = tc.args.get("chat_ids", plan.chat_ids)
                if chat_ids:
                    chat_ids = [int(x) for x in chat_ids]
                stats, meta = await tool_sql_stats_by_date(
                    chat_id=chat_id,
                    scope=scope,
                    chat_types=chat_types,
                    chat_ids=chat_ids,
                    resolved=resolved,
                )
                ctx.stats.update(stats)
                ctx.tool_runs.append(ToolRun(name=name, ok=True, meta=meta))
                continue

            if name == "rag_search":
                scope = PlanScope(tc.args.get("scope", plan.scope.value))
                chat_ids = tc.args.get("chat_ids", plan.chat_ids)
                if chat_ids:
                    chat_ids = [int(x) for x in chat_ids]
                top_k = int(tc.args.get("top_k", 8))
                q = str(tc.args.get("query") or query)
                chunks, meta = await tool_rag_search(chat_id=chat_id, scope=scope, chat_ids=chat_ids, query=q, top_k=top_k)
                ctx.chunks.extend(chunks)
                ctx.tool_runs.append(ToolRun(name=name, ok=True, meta=meta))
                continue

            if name == "sql_search_messages":
                scope = PlanScope(tc.args.get("scope", plan.scope.value))
                chat_types = tc.args.get("chat_types", plan.chat_types)
                if chat_types:
                    chat_types = [PlanChatType(x) for x in chat_types]
                chat_ids = tc.args.get("chat_ids", plan.chat_ids)
                if chat_ids:
                    chat_ids = [int(x) for x in chat_ids]
                lim = int(tc.args.get("limit", 30))
                q = str(tc.args.get("query") or query)
                msgs, meta = await tool_sql_search_messages(
                    chat_id=chat_id,
                    scope=scope,
                    chat_types=chat_types,
                    chat_ids=chat_ids,
                    query=q,
                    limit=lim,
                )
                ctx.messages.extend(msgs)
                ctx.tool_runs.append(ToolRun(name=name, ok=True, meta=meta))
                continue

            if name == "sql_messages_by_chat_query_and_date":
                if not resolved:
                    raise ValueError("time_range required")
                scope = PlanScope(tc.args.get("scope", plan.scope.value))
                chat_types = tc.args.get("chat_types", plan.chat_types)
                if chat_types:
                    chat_types = [PlanChatType(x) for x in chat_types]
                max_rows = int(tc.args.get("max_rows", 1500))
                chat_query = str(tc.args.get("chat_query") or "")
                msgs, meta = await tool_sql_messages_by_chat_query_and_date(
                    scope=scope,
                    chat_id=chat_id,
                    resolved=resolved,
                    chat_query=chat_query,
                    chat_types=chat_types,
                    max_rows=max_rows,
                )
                ctx.messages.extend(msgs)
                ctx.tool_runs.append(ToolRun(name=name, ok=True, meta=meta))
                continue

            ctx.tool_runs.append(ToolRun(name=name, ok=False, meta={"error": "unknown_tool"}))
        except Exception as exc:
            ctx.tool_runs.append(ToolRun(name=name, ok=False, meta={"error": str(exc)[:200]}))

    return ctx
