import logging

from aiogram import Bot, Router
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.llm.embedding import transcribe_audio_gemini
from app.llm.gemini_provider import GeminiContentError
from app.logic.reply import run_ai_reply
from app.services.interactions import set_feedback
from app.services.message_ingest import ingest_aiogram_incoming, ingest_aiogram_outgoing

logger = logging.getLogger(__name__)
router = Router()


def _feedback_kb(interaction_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="👍", callback_data=f"fb:like:{interaction_id}")
    kb.button(text="👎", callback_data=f"fb:dislike:{interaction_id}")
    kb.adjust(2)
    return kb.as_markup()


async def _llm_respond(msg: Message, question: str | None = None) -> None:
    thinking = await msg.answer("⏳")
    last_status = ""
    async def _progress(text: str) -> None:
        nonlocal last_status
        if not text or text == last_status:
            return
        last_status = text
        try:
            await thinking.edit_text(text)
        except Exception:
            pass
    try:
        if not question:
            await thinking.edit_text("Вопрос пустой.")
            return
        res = await run_ai_reply(
            chat_id=msg.chat.id,
            user_id=msg.from_user.id if msg.from_user else None,
            question=question,
            on_progress=_progress,
        )
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
        lower = err.lower()
        if "deepseek" in lower:
            if "insufficient balance" in lower or "402" in lower:
                await thinking.edit_text("⚠️ DeepSeek: недостаточно средств (402 Insufficient Balance). Пополни баланс или добавь другой токен.")
            elif "no active deepseek token" in lower:
                await thinking.edit_text("⚠️ Нет активных токенов DeepSeek с capability chat. Проверь в Настройки → API токены.")
            elif "rate-limited" in lower or "429" in lower:
                await thinking.edit_text("⚠️ Все токены DeepSeek на cooldown. Подожди минуту и попробуй снова.")
            else:
                await thinking.edit_text(f"⚠️ Ошибка DeepSeek: <code>{err[:150]}</code>")
        elif "no gemini tokens" in lower:
            await thinking.edit_text(
                "⚠️ Нет активных токенов Gemini для чата.\n"
                "Если ты хочешь отвечать через DeepSeek — поставь <code>LLM_PROVIDER=deepseek</code> и перезапусти."
            )
        elif "rate-limited" in lower or "429" in lower:
            await thinking.edit_text("⚠️ Все токены на cooldown (429). Подожди минуту и попробуй снова.")
        elif "http" in lower:
            await thinking.edit_text(f"⚠️ Ошибка API: <code>{err[:150]}</code>")
        elif "network" in lower:
            await thinking.edit_text("⚠️ Нет соединения с API. Проверь сеть.")
        else:
            await thinking.edit_text(f"❌ Неизвестная ошибка: <code>{err[:150]}</code>")
        return
    except Exception:
        logger.exception("Unexpected LLM error chat=%s", msg.chat.id)
        await thinking.edit_text("❌ Произошла непредвиденная ошибка. Попробуй позже.")
        return

    markup = _feedback_kb(res.interaction_id) if res.interaction_id else None
    await thinking.edit_text(res.text, reply_markup=markup)
    dialog_key = f"{msg.chat.id}:{msg.from_user.id}" if msg.from_user else f"{msg.chat.id}"
    await ingest_aiogram_outgoing(
        chat_id=msg.chat.id,
        telegram_msg_id=thinking.message_id,
        text=res.text,
        dialog_key=dialog_key,
    )


@router.callback_query(lambda c: bool(c.data) and c.data.startswith("fb:"))
async def on_feedback(cb: CallbackQuery) -> None:
    data = cb.data or ""
    parts = data.split(":")
    if len(parts) != 3:
        await cb.answer("Ошибка")
        return
    feedback = parts[1]
    try:
        interaction_id = int(parts[2])
    except ValueError:
        await cb.answer("Ошибка")
        return

    if feedback not in ("like", "dislike"):
        await cb.answer("Ошибка")
        return

    await set_feedback(interaction_id=interaction_id, feedback=feedback)
    await cb.answer("Сохранено")


@router.message()
async def handle_message(msg: Message, bot: Bot) -> None:
    try:
        inserted = await ingest_aiogram_incoming(msg)
    except Exception:
        logger.exception("Failed to save message chat_id=%s msg_id=%s", msg.chat.id, msg.message_id)
        inserted = True

    if msg.chat.type != ChatType.PRIVATE:
        return
    if not inserted:
        return

    question = msg.text or msg.caption

    voice_obj = msg.voice or msg.audio
    if not question and voice_obj:
        status = await msg.answer("🎤 распознаю…")
        try:
            buf = await bot.download(voice_obj)
            mime = voice_obj.mime_type or "audio/ogg"
            question = await transcribe_audio_gemini(mime_type=mime, data=buf.read())
            await status.delete()
        except Exception as exc:
            logger.warning("Voice input transcription failed chat=%s: %s", msg.chat.id, exc)
            await status.edit_text(f"⚠️ Не смог распознать голосовое: {str(exc)[:120]}")
            return

    if not question:
        return

    await _llm_respond(msg, question=question)
