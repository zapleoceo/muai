import logging

from telethon.errors import ChatIdInvalidError, ChannelPrivateError
from telethon.tl.functions.channels import GetForumTopicsRequest
from telethon.tl.types import Channel

from app.db.database import AsyncSessionLocal
from app.db.models import Chat, ChatTopic
from app.userbot.client import get_client
from sqlalchemy import select, delete
from sqlalchemy.dialects.postgresql import insert

logger = logging.getLogger(__name__)


async def _fetch_topics(entity) -> list[dict] | None:
    """Return list of {topic_id, title, is_closed} or None if not a forum."""
    if not isinstance(entity, Channel) or not getattr(entity, "forum", False):
        return None
    client = get_client()
    try:
        result = await client(GetForumTopicsRequest(
            channel=entity,
            q="",
            offset_date=0,
            offset_id=0,
            offset_topic=0,
            limit=100,
        ))
        return [
            {"topic_id": t.id, "title": t.title, "is_closed": bool(getattr(t, "closed", False))}
            for t in result.topics
        ]
    except Exception as exc:
        logger.debug("topics: could not fetch for %s: %s", getattr(entity, "title", "?"), exc)
        return None


async def sync_topics() -> int:
    """Fetch and store topics for all forum supergroups. Returns count of updated chats."""
    client = get_client()
    if not client.is_connected():
        raise RuntimeError("Userbot not connected")

    async with AsyncSessionLocal() as session:
        chats = (await session.execute(
            select(Chat).where(Chat.type.in_(["supergroup", "channel"]))
        )).scalars().all()

    updated = 0
    for chat in chats:
        try:
            entity = await client.get_entity(chat.id)
        except Exception:
            continue

        topics = await _fetch_topics(entity)
        if topics is None:
            continue

        async with AsyncSessionLocal() as session:
            await session.execute(delete(ChatTopic).where(ChatTopic.chat_id == chat.id))
            if topics:
                await session.execute(
                    insert(ChatTopic).values([
                        {"chat_id": chat.id, "topic_id": t["topic_id"],
                         "title": t["title"], "is_closed": t["is_closed"]}
                        for t in topics
                    ]).on_conflict_do_nothing()
                )
            await session.commit()

        logger.info("topics: %s → %d topics", chat.title, len(topics))
        updated += 1

    return updated
