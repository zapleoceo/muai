import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.db.database import AsyncSessionLocal
from app.db.models import ExecutorBot, ExecutorChat, ExecutorInbox

logger = logging.getLogger(__name__)


async def _find_executor_for_chat(chat_id: int) -> int | None:
    async with AsyncSessionLocal() as session:
        row = await session.execute(
            select(ExecutorChat.executor_id)
            .join(ExecutorBot, ExecutorBot.id == ExecutorChat.executor_id)
            .where(
                ExecutorChat.chat_id == chat_id,
                ExecutorBot.is_active == True,  # noqa: E712
                ExecutorBot.bot_token.isnot(None),
            )
            .limit(1)
        )
        return row.scalar_one_or_none()


async def _first_active_executor() -> int | None:
    """Fallback: pick any active executor when bot is not in the chat yet."""
    async with AsyncSessionLocal() as session:
        row = await session.execute(
            select(ExecutorBot.id)
            .where(
                ExecutorBot.is_active == True,  # noqa: E712
                ExecutorBot.bot_token.isnot(None),
            )
            .limit(1)
        )
        return row.scalar_one_or_none()


async def handle_owner_mention(
    *,
    chat_id: int,
    chat_title: str,
    sender_name: str,
    sender_id: int | None,
    message_text: str,
    tg_message_id: int,
    quoted_text: str | None = None,
    quoted_from: str | None = None,
) -> None:
    executor_id = await _find_executor_for_chat(chat_id)
    if executor_id is None:
        executor_id = await _first_active_executor()

    if executor_id is None:
        await _notify_no_executor(chat_title=chat_title, sender_name=sender_name, message_text=message_text)
        return

    async with AsyncSessionLocal() as session:
        item = ExecutorInbox(
            executor_id=executor_id,
            chat_id=chat_id,
            chat_title=chat_title,
            tg_message_id=tg_message_id,
            from_user_id=sender_id,
            from_user_name=sender_name,
            text=message_text,
            is_mention=True,
            quoted_text=quoted_text,
            quoted_from=quoted_from,
            priority="HIGH",
        )
        session.add(item)
        await session.commit()
        item_id = item.id

    from app.main import bot
    from app.services.inbox_processor import process_new_item
    asyncio.create_task(process_new_item(item_id, bot))
    logger.info(
        "Owner mention in chat=%s → executor_id=%d inbox_item=%d",
        chat_id, executor_id, item_id,
    )


async def _notify_no_executor(*, chat_title: str, sender_name: str, message_text: str) -> None:
    from app.main import bot
    from app.config import get_settings
    settings = get_settings()
    text = (
        f"📬 <b>Вас упомянули в «{chat_title}»</b>\n"
        f"👤 <b>{sender_name}:</b> {message_text}\n\n"
        f"⚠️ <i>Нет активного executor-бота для ответа. "
        f"Добавьте бота через вкладку «Боты».</i>"
    )
    try:
        await bot.send_message(chat_id=settings.owner_telegram_id, text=text)
    except Exception as exc:
        logger.warning("mention_alert: failed to notify owner: %s", exc)
