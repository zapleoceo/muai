import logging
from datetime import datetime, timezone

import httpx
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from app.db.database import AsyncSessionLocal
from app.db.models import ExecutorBot, ExecutorInbox

logger = logging.getLogger(__name__)


def _build_query(item) -> str:
    parts: list[str] = []

    if item.context_messages:
        lines = [
            f"{m['from']}: {m['text']}"
            for m in item.context_messages
            if m.get("text") and m.get("from")
        ]
        if lines:
            parts.append("[Контекст чата]:\n" + "\n".join(lines))

    if item.quoted_text:
        who = item.quoted_from or "неизвестный"
        parts.append(f"[Цитируемое сообщение от {who}]: «{item.quoted_text}»")

    parts.append(item.text or "")
    return "\n\n".join(p for p in parts if p)


async def process_new_item(item_id: int, bot: Bot) -> None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(ExecutorInbox).where(ExecutorInbox.id == item_id))
        item = result.scalar_one_or_none()
        if not item or item.status != "pending":
            return

        from app.services.answer_pipeline import run_answer_pipeline  # avoid circular
        try:
            query = _build_query(item)
            result = await run_answer_pipeline(
                query=query,
                chat_id=item.chat_id,
                user_id=item.from_user_id,
            )
            draft = result.text or ""
        except Exception as exc:
            logger.warning("Draft generation failed for item %d: %s", item_id, exc)
            draft = ""

        item.draft_reply = draft
        item.status = "notified"
        item.processed_at = datetime.now(timezone.utc)
        await session.commit()

    from app.config import get_settings
    settings = get_settings()

    text = (
        f"📣 <b>Упоминание в «{item.chat_title or item.chat_id}»</b>\n"
        f"👤 <b>{item.from_user_name or 'Unknown'}:</b> {item.text or ''}\n\n"
        f"💬 <b>Предлагаю ответить:</b>\n{draft}"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Отправить", callback_data=f"exec_approve:{item_id}"),
        InlineKeyboardButton(text="❌ Игнорировать", callback_data=f"exec_ignore:{item_id}"),
    ]])

    try:
        sent = await bot.send_message(
            chat_id=settings.owner_telegram_id,
            text=text,
            reply_markup=keyboard,
        )
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(ExecutorInbox).where(ExecutorInbox.id == item_id))
            item = result.scalar_one_or_none()
            if item:
                item.owner_notif_msg_id = sent.message_id
                await session.commit()
    except Exception:
        logger.exception("Failed to notify owner for inbox item %d", item_id)


async def send_via_executor(item_id: int, bot: Bot, override_text: str | None = None) -> bool:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(ExecutorInbox).where(ExecutorInbox.id == item_id))
        item = result.scalar_one_or_none()
        if not item:
            return False

        exec_result = await session.execute(
            select(ExecutorBot).where(ExecutorBot.id == item.executor_id)
        )
        executor = exec_result.scalar_one_or_none()
        if not executor:
            return False

    text = override_text or item.draft_reply or ""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{executor.api_url}/send",
                json={
                    "chat_id": item.chat_id,
                    "text": text,
                    "reply_to_message_id": item.tg_message_id,
                },
                headers={"Authorization": f"Bearer {executor.api_secret}"},
            )
            r.raise_for_status()
    except Exception:
        logger.exception("send_via_executor failed for item %d", item_id)
        return False

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(ExecutorInbox).where(ExecutorInbox.id == item_id))
        item = result.scalar_one_or_none()
        if item:
            item.status = "replied"
            await session.commit()

    if item and item.owner_notif_msg_id:
        from app.config import get_settings
        settings = get_settings()
        try:
            await bot.edit_message_text(
                chat_id=settings.owner_telegram_id,
                message_id=item.owner_notif_msg_id,
                text="✅ Отправлено",
            )
        except Exception:
            logger.warning("Could not edit owner notification for item %d", item_id)

    return True


async def mark_ignored(item_id: int, bot: Bot) -> None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(ExecutorInbox).where(ExecutorInbox.id == item_id))
        item = result.scalar_one_or_none()
        if not item:
            return
        item.status = "ignored"
        await session.commit()

    if item and item.owner_notif_msg_id:
        from app.config import get_settings
        settings = get_settings()
        try:
            await bot.edit_message_text(
                chat_id=settings.owner_telegram_id,
                message_id=item.owner_notif_msg_id,
                text="❌ Проигнорировано",
            )
        except Exception:
            logger.warning("Could not edit owner notification for item %d", item_id)
