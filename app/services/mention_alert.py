import logging

from app.llm.base import LLMMessage

logger = logging.getLogger(__name__)


async def _analyze_action(message_text: str, chat_title: str, sender_name: str) -> str:
    from app.llm.factory import get_llm_provider
    provider = get_llm_provider()
    prompt = (
        f"Сообщение в чате «{chat_title}» от {sender_name}:\n"
        f"«{message_text}»\n\n"
        "Какие конкретные действия нужно предпринять? "
        "Перечисли кратко (1–3 пункта маркированным списком).\n"
        "Если нет конкретных задач — напиши «Информационное сообщение».\n"
        "Отвечай на русском, без вступлений."
    )
    try:
        result = await provider.complete([LLMMessage(role="user", content=prompt)])
        return result or "—"
    except Exception as exc:
        logger.warning("mention_alert: LLM error: %s", exc)
        return "—"


async def notify_owner_mention(
    *,
    chat_id: int,
    chat_title: str,
    sender_name: str,
    message_text: str,
) -> None:
    from app.main import bot
    from app.config import get_settings
    settings = get_settings()

    analysis = await _analyze_action(message_text, chat_title, sender_name)

    text = (
        f"📬 <b>Вас упомянули в «{chat_title}»</b>\n"
        f"👤 <b>{sender_name}:</b> {message_text}\n\n"
        f"🤖 <b>Что нужно сделать:</b>\n{analysis}"
    )

    try:
        await bot.send_message(chat_id=settings.owner_telegram_id, text=text)
    except Exception as exc:
        logger.warning("mention_alert: failed to notify owner: %s", exc)
