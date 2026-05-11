import logging

from aiogram import Bot, Router
from aiogram.enums import ChatType
from aiogram.types import Message

from app.bot.storage import save_incoming, save_outgoing
from app.logic.reply import run_ai_reply

logger = logging.getLogger(__name__)
router = Router()


@router.message()
async def handle_message(msg: Message, bot: Bot) -> None:
    try:
        await save_incoming(msg)
    except Exception:
        logger.exception("Failed to save message chat_id=%s msg_id=%s", msg.chat.id, msg.message_id)

    # Auto-reply in private chats only; groups still use /ai
    if msg.chat.type != ChatType.PRIVATE:
        return
    if not (msg.text or msg.caption):
        return

    thinking = await msg.answer("⏳")
    try:
        reply_text = await run_ai_reply(chat_id=msg.chat.id)
    except Exception as exc:
        logger.exception("LLM error chat_id=%s", msg.chat.id)
        err = str(exc)
        if "blocked" in err or "empty response" in err:
            await thinking.edit_text("⚠️ Gemini заблокировал ответ.")
        elif "rate-limited" in err or "429" in err:
            await thinking.edit_text("⚠️ Все токены на cooldown, попробуй позже.")
        elif "No Gemini tokens" in err:
            await thinking.edit_text("⚠️ Нет токенов Gemini.")
        else:
            await thinking.edit_text(f"❌ {err[:120]}")
        return

    await thinking.edit_text(reply_text)
    dialog_key = f"{msg.chat.id}:{msg.from_user.id}" if msg.from_user else f"{msg.chat.id}"
    await save_outgoing(
        chat_id=msg.chat.id,
        telegram_msg_id=thinking.message_id,
        text=reply_text,
        dialog_key=dialog_key,
    )
