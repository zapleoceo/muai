from datetime import datetime, timezone

from sqlalchemy import delete, select

from app.db.database import AsyncSessionLocal
from app.db.models import ExecutorBot, ExecutorChat


async def register_or_update(
    *,
    name: str,
    bot_username: str,
    api_url: str,
    api_secret: str,
    chats: list[dict],
) -> int:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ExecutorBot).where(ExecutorBot.bot_username == bot_username)
        )
        executor = result.scalar_one_or_none()

        now = datetime.now(timezone.utc)
        if executor:
            executor.name = name
            executor.api_url = api_url
            executor.api_secret = api_secret
            executor.is_active = True
            executor.last_seen_at = now
        else:
            executor = ExecutorBot(
                name=name,
                bot_username=bot_username,
                api_url=api_url,
                api_secret=api_secret,
                last_seen_at=now,
            )
            session.add(executor)
            await session.flush()

        executor_id: int = executor.id

        await session.execute(
            delete(ExecutorChat).where(ExecutorChat.executor_id == executor_id)
        )

        for chat in chats:
            session.add(ExecutorChat(
                executor_id=executor_id,
                chat_id=chat["chat_id"],
                chat_title=chat.get("chat_title"),
                chat_type=chat.get("chat_type"),
                can_send=chat.get("can_send", True),
            ))

        await session.commit()
        return executor_id


async def touch(executor_id: int) -> None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ExecutorBot).where(ExecutorBot.id == executor_id)
        )
        executor = result.scalar_one_or_none()
        if executor:
            executor.last_seen_at = datetime.now(timezone.utc)
            await session.commit()


async def update_bot_settings(
    bot_id: int,
    *,
    forward_mode: str | None = None,
    is_enabled: bool | None = None,
) -> None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(ExecutorBot).where(ExecutorBot.id == bot_id))
        bot = result.scalar_one_or_none()
        if not bot:
            raise ValueError(f"Executor bot {bot_id} not found")
        if forward_mode is not None:
            bot.forward_mode = forward_mode
        if is_enabled is not None:
            bot.is_active = is_enabled
        await session.commit()


async def get_executor_for_chat(chat_id: int) -> dict | None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ExecutorBot, ExecutorChat)
            .join(ExecutorChat, ExecutorChat.executor_id == ExecutorBot.id)
            .where(
                ExecutorChat.chat_id == chat_id,
                ExecutorChat.can_send == True,  # noqa: E712
                ExecutorBot.is_active == True,  # noqa: E712
            )
            .limit(1)
        )
        row = result.first()
        if not row:
            return None
        bot, _ = row
        return {"id": bot.id, "name": bot.name, "api_url": bot.api_url, "api_secret": bot.api_secret}


async def find_accessible_chats(chat_query: str) -> list[dict]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ExecutorChat, ExecutorBot)
            .join(ExecutorBot, ExecutorBot.id == ExecutorChat.executor_id)
            .where(
                ExecutorChat.chat_title.ilike(f"%{chat_query}%"),
                ExecutorBot.is_active == True,  # noqa: E712
            )
        )
        rows = result.all()
        return [
            {
                "executor_id": bot.id,
                "executor_name": bot.name,
                "chat_id": chat.chat_id,
                "chat_title": chat.chat_title,
                "chat_type": chat.chat_type,
            }
            for chat, bot in rows
        ]


async def list_executors() -> list[dict]:
    async with AsyncSessionLocal() as session:
        bots_result = await session.execute(select(ExecutorBot).order_by(ExecutorBot.id))
        bots = bots_result.scalars().all()

        out = []
        for bot in bots:
            chats_result = await session.execute(
                select(ExecutorChat).where(ExecutorChat.executor_id == bot.id)
            )
            chats = chats_result.scalars().all()
            out.append({
                "id": bot.id,
                "name": bot.name,
                "bot_username": bot.bot_username,
                "api_url": bot.api_url,
                "is_active": bot.is_active,
                "forward_mode": bot.forward_mode or "mentions",
                "last_seen_at": bot.last_seen_at.isoformat() if bot.last_seen_at else None,
                "created_at": bot.created_at.isoformat() if bot.created_at else None,
                "chats": [
                    {
                        "chat_id": c.chat_id,
                        "chat_title": c.chat_title,
                        "chat_type": c.chat_type,
                        "can_send": c.can_send,
                    }
                    for c in chats
                ],
            })
        return out
