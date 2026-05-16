from __future__ import annotations

import re

from sqlalchemy import case, or_, select

from app.db.models import Chat, Message
from app.services.answering_types import PlanChatType
from app.services.plan_executor.links import build_message_link


def _msg_row(m: Message, c: Chat) -> dict:
    return {
        "chat_id": int(m.chat_id),
        "chat": {"id": int(m.chat_id), "type": c.type, "title": c.title, "username": c.username, "folder": getattr(c, "folder", None)},
        "message_id": int(m.id),
        "telegram_msg_id": int(m.telegram_msg_id) if m.telegram_msg_id is not None else None,
        "direction": m.direction,
        "role": "me" if m.direction == "out" else "them",
        "media_type": m.media_type or None,
        "text": m.text or m.caption or (f"[{m.media_type}]" if m.media_type else "[media]"),
        "date_utc": m.date_utc.isoformat() if m.date_utc else None,
        "link": build_message_link(
            chat_id=int(m.chat_id),
            chat_type=c.type,
            chat_username=c.username,
            telegram_msg_id=int(m.telegram_msg_id) if m.telegram_msg_id is not None else None,
        ),
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


def _has_cyrillic(s: str) -> bool:
    return bool(re.search(r"[Ѐ-ӿ]", s))


def _has_latin(s: str) -> bool:
    return bool(re.search(r"[A-Za-z]", s))
