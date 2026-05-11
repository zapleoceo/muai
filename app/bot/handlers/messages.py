import logging

from aiogram import Bot, Router
from aiogram.enums import ChatType
from aiogram.types import Message

from app.bot.storage import save_incoming, save_outgoing
from app.llm.gemini_provider import GeminiContentError
from app.logic.reply import run_ai_reply

logger = logging.getLogger(__name__)
router = Router()


async def _llm_respond(msg: Message, question: str | None = None) -> None:
    thinking = await msg.answer("⏳")
    try:
        reply_text = await run_ai_reply(chat_id=msg.chat.id, question=question)
    except GeminiContentError as exc:
        logger.warning("Gemini content block chat=%s: %s", msg.chat.id, exc.reason)
        await thinking.edit_text(
            f"⚠️ Gemini не смог ответить.\n"
            f"Причина: <code>{exc.reason}</code>\n\n"
            "Попробуй переформулировать вопрос."
        )
        return
    except RuntimeError as exc:
        err = str(exc)
        logger.error("LLM error chat=%s: %s", msg.chat.id, err)
        if "rate-limited" in err or "429" in err:
            await thinking.edit_text("⚠️ Все токены Gemini на cooldown. Подожди минуту и попробуй снова.")
        elif "No Gemini tokens" in err:
            await thinking.edit_text("⚠️ Нет активных токенов Gemini. Добавь на дашборде.")
        elif "HTTP" in err:
            await thinking.edit_text(f"⚠️ Ошибка API Gemini: <code>{err[:150]}</code>")
        elif "network" in err:
            await thinking.edit_text("⚠️ Нет соединения с Gemini API. Проверь сеть.")
        else:
            await thinking.edit_text(f"❌ Неизвестная ошибка: <code>{err[:150]}</code>")
        return
    except Exception as exc:
        logger.exception("Unexpected LLM error chat=%s", msg.chat.id)
        await thinking.edit_text(f"❌ Неожиданная ошибка: <code>{str(exc)[:150]}</code>")
        return

    await thinking.edit_text(reply_text)
    dialog_key = f"{msg.chat.id}:{msg.from_user.id}" if msg.from_user else f"{msg.chat.id}"
    await save_outgoing(
        chat_id=msg.chat.id,
        telegram_msg_id=thinking.message_id,
        text=reply_text,
        dialog_key=dialog_key,
    )


@router.message()
async def handle_message(msg: Message, bot: Bot) -> None:
    try:
        await save_incoming(msg)
    except Exception:
        logger.exception("Failed to save message chat_id=%s msg_id=%s", msg.chat.id, msg.message_id)

    # Auto-reply in private chats only; groups use /ai
    if msg.chat.type != ChatType.PRIVATE:
        return
    if not (msg.text or msg.caption):
        return

    await _llm_respond(msg, question=msg.text or msg.caption)
