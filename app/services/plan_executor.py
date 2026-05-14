from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import re
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, select, text, or_

from app.db.database import AsyncSessionLocal
from app.db.models import Chat, Message
from app.db.repository import MessageRepo
from app.llm.embedding import embed_text
from app.services.answering_types import Plan, PlanChatType, PlanScope, PlanTimeRange, RetrievedContext, ToolRun


@dataclass(frozen=True)
class ResolvedRange:
    from_utc: datetime
    to_utc: datetime


async def ensure_search_infra() -> None:
    ddl = [
        "CREATE EXTENSION IF NOT EXISTS pg_trgm",
        "CREATE INDEX IF NOT EXISTS idx_messages_fts_simple ON messages USING GIN (to_tsvector('simple', coalesce(text,'') || ' ' || coalesce(caption,'')))",
        "CREATE INDEX IF NOT EXISTS idx_messages_fts_ru ON messages USING GIN (to_tsvector('russian', coalesce(text,'') || ' ' || coalesce(caption,'')))",
        "CREATE INDEX IF NOT EXISTS idx_messages_fts_en ON messages USING GIN (to_tsvector('english', coalesce(text,'') || ' ' || coalesce(caption,'')))",
        "CREATE INDEX IF NOT EXISTS idx_messages_trgm ON messages USING GIN ((coalesce(text,'') || ' ' || coalesce(caption,'')) gin_trgm_ops)",
        "CREATE INDEX IF NOT EXISTS idx_chats_title_trgm ON chats USING GIN (coalesce(title,'') gin_trgm_ops)",
        "CREATE INDEX IF NOT EXISTS idx_chats_username_trgm ON chats USING GIN (coalesce(username,'') gin_trgm_ops)",
        "CREATE INDEX IF NOT EXISTS idx_chats_folder_trgm ON chats USING GIN (coalesce(folder,'') gin_trgm_ops)",
    ]
    async with AsyncSessionLocal() as session:
        for stmt in ddl:
            await session.execute(text(stmt))
        await session.commit()


async def ensure_chunk_schema() -> None:
    ddl = [
        "CREATE EXTENSION IF NOT EXISTS vector",
        "ALTER TABLE message_chunks ADD COLUMN IF NOT EXISTS min_msg_id bigint",
        "ALTER TABLE message_chunks ADD COLUMN IF NOT EXISTS msg_count integer",
        "ALTER TABLE message_chunks ADD COLUMN IF NOT EXISTS meta jsonb",
        "CREATE INDEX IF NOT EXISTS idx_chunks_min_msg_id ON message_chunks (min_msg_id)",
        "CREATE INDEX IF NOT EXISTS idx_chunks_max_msg_id ON message_chunks (max_msg_id)",
        "CREATE INDEX IF NOT EXISTS idx_chunks_embedding_hnsw ON message_chunks USING hnsw (embedding vector_cosine_ops)",
        """
        CREATE TABLE IF NOT EXISTS media_chunks (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL REFERENCES chats(id),
            chat_title TEXT,
            chat_username TEXT,
            source_msg_id BIGINT NOT NULL,
            source_tg_msg_id BIGINT,
            media_type TEXT NOT NULL,
            date_utc TIMESTAMPTZ,
            chunk_text TEXT NOT NULL,
            embedding vector(768),
            meta jsonb,
            created_at TIMESTAMPTZ DEFAULT now(),
            CONSTRAINT uq_media_chunks_chat_tg_msg UNIQUE (chat_id, source_tg_msg_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_media_chunks_chat ON media_chunks (chat_id)",
        "CREATE INDEX IF NOT EXISTS idx_media_chunks_date ON media_chunks (date_utc)",
        "CREATE INDEX IF NOT EXISTS idx_media_chunks_embedding_hnsw ON media_chunks USING hnsw (embedding vector_cosine_ops)",
    ]
    async with AsyncSessionLocal() as session:
        for stmt in ddl:
            await session.execute(text(stmt))
        await session.commit()


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
    elif time_range == PlanTimeRange.LAST_30_DAYS:
        start_local = datetime.combine(today_local - timedelta(days=29), time(0, 0), tzinfo=zone)
        end_local = datetime.combine(today_local + timedelta(days=1), time(0, 0), tzinfo=zone)
    elif time_range == PlanTimeRange.ALL_TIME:
        start_local = datetime(1970, 1, 1, 0, 0, tzinfo=zone)
        end_local = now_local + timedelta(seconds=1)
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
                "role": "me" if m.direction == "out" else "them",
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
        text_rows = await MessageRepo(session).search_chunks(
            q_vec,
            limit=top_k,
            chat_id=chat_id if scope == PlanScope.CURRENT_CHAT else None,
            chat_ids=chat_ids if scope != PlanScope.CURRENT_CHAT else None,
        )
        media_rows = await MessageRepo(session).search_media_chunks(
            q_vec,
            limit=top_k,
            chat_id=chat_id if scope == PlanScope.CURRENT_CHAT else None,
            chat_ids=chat_ids if scope != PlanScope.CURRENT_CHAT else None,
        )

    candidates: list[dict] = []
    for r in text_rows:
        dist = float(getattr(r, "distance", 0.0) or 0.0)
        candidates.append(
            {
                "kind": "text",
                "score": dist,
                "chunk_id": int(r.id),
                "chat_id": int(r.chat_id),
                "chat_title": r.chat_title,
                "text": r.chunk_text,
                "msg_date_from": r.msg_date_from.isoformat() if getattr(r, "msg_date_from", None) else None,
                "msg_date_to": r.msg_date_to.isoformat() if getattr(r, "msg_date_to", None) else None,
                "chat_username": getattr(r, "chat_username", None),
                "max_tg_msg_id": int(getattr(r, "max_tg_msg_id", 0) or 0) or None,
                "min_msg_id": int(getattr(r, "min_msg_id", 0) or 0) or None,
                "max_msg_id": int(getattr(r, "max_msg_id", 0) or 0) or None,
                "msg_count": int(getattr(r, "msg_count", 0) or 0) or None,
                "meta": getattr(r, "meta", None),
                "link": build_message_link(
                    chat_id=int(r.chat_id),
                    chat_type=None,
                    chat_username=getattr(r, "chat_username", None),
                    telegram_msg_id=int(getattr(r, "max_tg_msg_id", 0) or 0) or None,
                ),
            }
        )

    for r in media_rows:
        dist = float(getattr(r, "distance", 0.0) or 0.0)
        source_tg_msg_id = int(getattr(r, "source_tg_msg_id", 0) or 0) or None
        candidates.append(
            {
                "kind": "media",
                "score": dist,
                "chunk_id": -int(r.id),
                "chat_id": int(r.chat_id),
                "chat_title": getattr(r, "chat_title", None),
                "text": r.chunk_text,
                "msg_date_from": getattr(r, "date_utc", None).isoformat() if getattr(r, "date_utc", None) else None,
                "msg_date_to": getattr(r, "date_utc", None).isoformat() if getattr(r, "date_utc", None) else None,
                "chat_username": getattr(r, "chat_username", None),
                "max_tg_msg_id": source_tg_msg_id,
                "min_msg_id": None,
                "max_msg_id": None,
                "msg_count": 1,
                "meta": getattr(r, "meta", None),
                "link": build_message_link(
                    chat_id=int(r.chat_id),
                    chat_type=None,
                    chat_username=getattr(r, "chat_username", None),
                    telegram_msg_id=source_tg_msg_id,
                ),
            }
        )

    candidates.sort(key=lambda x: float(x.get("score") or 0.0))
    items = candidates[: max(0, int(top_k))]
    return items, {
        "count": len(items),
        "top_k": top_k,
        "text_count": len(text_rows),
        "media_count": len(media_rows),
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
        )
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
                    "folder": c.folder,
                },
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
        )
    return items, {
        "count": len(items),
        "limit": limit,
        "query": q_raw,
        "from_utc": resolved.from_utc.isoformat(),
        "to_utc": resolved.to_utc.isoformat(),
    }


async def tool_sql_recent_messages_by_chat_query(
    *,
    scope: PlanScope,
    chat_id: int,
    chat_query: str,
    chat_types: list[PlanChatType] | None,
    limit: int,
) -> tuple[list[dict], dict]:
    q_raw = str(chat_query or "").strip().strip('"').strip("'")
    q_norm = q_raw.lstrip("@")
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

        q = (
            select(Message, Chat)
            .join(Chat, Chat.id == Message.chat_id)
            .where(Message.chat_id == selected_chat.id)
            .order_by(Message.date_utc.desc())
            .limit(limit)
        )
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
                    "folder": c.folder,
                },
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
        )
    return items, {
        "count": len(items),
        "limit": limit,
        "chat_query": q_norm,
        "selected_chat": {
            "id": int(selected_chat.id),
            "type": selected_chat.type,
            "title": selected_chat.title,
            "username": selected_chat.username,
            "folder": selected_chat.folder,
        },
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

    items = []
    for (c, s) in rows:
        items.append(
            {
                "chat_id": int(c.id),
                "type": c.type,
                "title": c.title,
                "username": c.username,
                "folder": c.folder,
                "score": int(s or 0),
            }
        )
    return items, {"count": len(items), "limit": limit, "query": q_raw}


def _has_cyrillic(s: str) -> bool:
    return bool(re.search(r"[\u0400-\u04FF]", s))


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
    q_expr = "coalesce(m.text,'') || ' ' || coalesce(m.caption,'')"

    params: dict[str, object] = {
        "q": q_raw,
        "lim": int(limit),
        "use_ru": bool(use_ru),
        "use_en": bool(use_en),
    }

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

    sql = text(
        f"""
        WITH q AS (
          SELECT
            websearch_to_tsquery('simple', :q)  AS qs,
            websearch_to_tsquery('russian', :q) AS qru,
            websearch_to_tsquery('english', :q) AS qen
        )
        SELECT
          m.id AS message_id,
          m.chat_id,
          m.telegram_msg_id,
          m.direction,
          m.text,
          m.caption,
          m.media_type,
          m.date_utc,
          c.type AS chat_type,
          c.title AS chat_title,
          c.username AS chat_username,
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
        """
    )

    async with AsyncSessionLocal() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        await session.execute(text("SELECT set_limit(0.12)"))
        rows = (await session.execute(sql, params)).fetchall()

    items: list[dict] = []
    for r in rows:
        msg_text = r.text or r.caption or f"[{r.media_type or 'media'}]"
        items.append(
            {
                "chat_id": int(r.chat_id),
                "chat": {"id": int(r.chat_id), "type": r.chat_type, "title": r.chat_title, "username": r.chat_username},
                "message_id": int(r.message_id),
                "telegram_msg_id": int(r.telegram_msg_id) if r.telegram_msg_id is not None else None,
                "direction": r.direction,
                "role": "me" if r.direction == "out" else "them",
                "text": msg_text,
                "date_utc": r.date_utc.isoformat() if r.date_utc else None,
                "link": build_message_link(
                    chat_id=int(r.chat_id),
                    chat_type=r.chat_type,
                    chat_username=r.chat_username,
                    telegram_msg_id=int(r.telegram_msg_id) if r.telegram_msg_id is not None else None,
                ),
                "score": float((r.r_simple or 0) + (r.r_ru or 0) + (r.r_en or 0) + (r.sim or 0)),
                "score_parts": {
                    "simple": float(r.r_simple or 0),
                    "ru": float(r.r_ru or 0),
                    "en": float(r.r_en or 0),
                    "trgm": float(r.sim or 0),
                },
            }
        )

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

        q = (
            select(Message, Chat)
            .join(Chat, Chat.id == Message.chat_id)
            .where(Message.chat_id == c.id, Message.telegram_msg_id == int(telegram_msg_id))
            .limit(5)
        )
        rows = (await session.execute(q)).all()

    items = []
    for (m, chat) in rows:
        items.append(
            {
                "chat_id": int(m.chat_id),
                "chat": {"id": int(m.chat_id), "type": chat.type, "title": chat.title, "username": chat.username},
                "message_id": int(m.id),
                "telegram_msg_id": int(m.telegram_msg_id) if m.telegram_msg_id is not None else None,
                "direction": m.direction,
                "role": "me" if m.direction == "out" else "them",
                "text": m.text or m.caption or f"[{m.media_type or 'media'}]",
                "date_utc": m.date_utc.isoformat() if m.date_utc else None,
                "link": build_message_link(
                    chat_id=int(m.chat_id),
                    chat_type=chat.type,
                    chat_username=chat.username,
                    telegram_msg_id=int(m.telegram_msg_id) if m.telegram_msg_id is not None else None,
                ),
            }
        )
    return items, {"count": len(items), "chat_username": getattr(c, "username", None), "chat_id": int(getattr(c, "id", 0) or 0) or None, "telegram_msg_id": int(telegram_msg_id)}

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
        )
    return items, {
        "count": len(items),
        "max_rows": max_rows,
        "chat_query": q_norm,
        "from_utc": resolved.from_utc.isoformat(),
        "to_utc": resolved.to_utc.isoformat(),
    }


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
                    "folder": c.folder,
                },
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
        )
    return items, {
        "count": len(items),
        "max_rows": max_rows,
        "folder": folder_raw,
        "from_utc": resolved.from_utc.isoformat(),
        "to_utc": resolved.to_utc.isoformat(),
    }


_ALLOWED_TOOLS: dict[str, set[str]] = {
    "INFO_ONLY": {"get_recent_dialog"},
    "RAG_SEMANTIC": {"get_recent_dialog", "rag_search", "sql_search_messages", "sql_find_chats", "sql_lex_search_messages"},
    "SQL_DATE_SUMMARY": {"get_recent_dialog", "sql_messages_by_date", "sql_messages_by_chat_query_and_date", "sql_messages_by_folder_and_date", "sql_stats_by_date", "sql_search_messages", "sql_search_messages_by_date", "sql_find_chats", "sql_lex_search_messages", "sql_message_by_tg_ref", "sql_recent_messages_by_chat_query"},
    "HYBRID": {"get_recent_dialog", "rag_search", "sql_messages_by_date", "sql_messages_by_chat_query_and_date", "sql_messages_by_folder_and_date", "sql_stats_by_date", "sql_search_messages", "sql_search_messages_by_date", "sql_find_chats", "sql_lex_search_messages", "sql_message_by_tg_ref", "sql_recent_messages_by_chat_query"},
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

            if name == "sql_find_chats":
                lim = int(tc.args.get("limit", 10))
                q = str(tc.args.get("query") or query)
                chat_types = tc.args.get("chat_types", plan.chat_types)
                if chat_types:
                    chat_types = [PlanChatType(x) for x in chat_types]
                items, meta = await tool_sql_find_chats(query=q, limit=lim, chat_types=chat_types)
                ctx.meta.setdefault("chat_candidates", []).extend(items)
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

            if name == "sql_search_messages_by_date":
                if not resolved:
                    raise ValueError("time_range required")
                scope = PlanScope(tc.args.get("scope", plan.scope.value))
                chat_types = tc.args.get("chat_types", plan.chat_types)
                if chat_types:
                    chat_types = [PlanChatType(x) for x in chat_types]
                chat_ids = tc.args.get("chat_ids", plan.chat_ids)
                if chat_ids:
                    chat_ids = [int(x) for x in chat_ids]
                lim = int(tc.args.get("limit", 50))
                q = str(tc.args.get("query") or query)
                msgs, meta = await tool_sql_search_messages_by_date(
                    chat_id=chat_id,
                    scope=scope,
                    chat_types=chat_types,
                    chat_ids=chat_ids,
                    resolved=resolved,
                    query=q,
                    limit=lim,
                )
                ctx.messages.extend(msgs)
                ctx.tool_runs.append(ToolRun(name=name, ok=True, meta=meta))
                continue

            if name == "sql_recent_messages_by_chat_query":
                scope = PlanScope(tc.args.get("scope", plan.scope.value))
                chat_types = tc.args.get("chat_types", plan.chat_types)
                if chat_types:
                    chat_types = [PlanChatType(x) for x in chat_types]
                lim = int(tc.args.get("limit", 5))
                cq = str(tc.args.get("chat_query") or "")
                msgs, meta = await tool_sql_recent_messages_by_chat_query(
                    scope=scope,
                    chat_id=chat_id,
                    chat_query=cq,
                    chat_types=chat_types,
                    limit=lim,
                )
                ctx.messages.extend(msgs)
                ctx.tool_runs.append(ToolRun(name=name, ok=True, meta=meta))
                continue

            if name == "sql_lex_search_messages":
                scope = PlanScope(tc.args.get("scope", plan.scope.value))
                chat_types = tc.args.get("chat_types", plan.chat_types)
                if chat_types:
                    chat_types = [PlanChatType(x) for x in chat_types]
                chat_ids = tc.args.get("chat_ids", plan.chat_ids)
                if chat_ids:
                    chat_ids = [int(x) for x in chat_ids]
                lim = int(tc.args.get("limit", 50))
                q = str(tc.args.get("query") or query)
                use_time = bool(tc.args.get("use_time_range", False))
                msgs, meta = await tool_sql_lex_search_messages(
                    chat_id=chat_id,
                    scope=scope,
                    chat_types=chat_types,
                    chat_ids=chat_ids,
                    query=q,
                    limit=lim,
                    resolved=resolved if (use_time and resolved) else None,
                )
                ctx.messages.extend(msgs)
                ctx.tool_runs.append(ToolRun(name=name, ok=True, meta=meta))
                continue

            if name == "sql_message_by_tg_ref":
                chat_username = str(tc.args.get("chat_username") or "")
                chat_id_arg = tc.args.get("chat_id")
                chat_id_val = int(chat_id_arg) if chat_id_arg is not None and str(chat_id_arg).strip() else None
                telegram_msg_id = int(tc.args.get("telegram_msg_id") or 0)
                msgs, meta = await tool_sql_message_by_tg_ref(chat_username=chat_username or None, chat_id=chat_id_val, telegram_msg_id=telegram_msg_id)
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

            if name == "sql_messages_by_folder_and_date":
                if not resolved:
                    raise ValueError("time_range required")
                scope = PlanScope(tc.args.get("scope", plan.scope.value))
                chat_types = tc.args.get("chat_types", plan.chat_types)
                if chat_types:
                    chat_types = [PlanChatType(x) for x in chat_types]
                max_rows = int(tc.args.get("max_rows", 1500))
                folder = str(tc.args.get("folder") or "")
                msgs, meta = await tool_sql_messages_by_folder_and_date(
                    scope=scope,
                    chat_id=chat_id,
                    resolved=resolved,
                    folder=folder,
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
