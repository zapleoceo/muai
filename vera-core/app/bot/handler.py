import logging

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

from app.bot.sender import reply
from app.config import get_settings
from app.orchestrator.pipeline import run

log = logging.getLogger(__name__)
router = Router()


def _is_mention(message: Message, bot_username: str) -> bool:
    if message.entities is None:
        return False
    for entity in message.entities:
        if entity.type == "mention":
            mention = message.text[entity.offset : entity.offset + entity.length] if message.text else ""
            if mention.lower() == f"@{bot_username.lower()}":
                return True
    return False


def _strip_mention(text: str, bot_username: str) -> str:
    prefix = f"@{bot_username}"
    stripped = text.strip()
    if stripped.lower().startswith(prefix.lower()):
        stripped = stripped[len(prefix):].strip()
    return stripped or text.strip()


@router.message()
async def handle_message(message: Message, bot: Bot) -> None:
    if message.text is None:
        return

    settings = get_settings()
    me = await bot.get_me()
    in_group = message.chat.id == settings.vera_group_id
    from_owner = message.from_user and message.from_user.id == settings.owner_telegram_id

    mentioned = _is_mention(message, me.username or "")
    replied_to_bot = (
        message.reply_to_message is not None
        and message.reply_to_message.from_user is not None
        and message.reply_to_message.from_user.id == me.id
    )

    if in_group and not (mentioned or replied_to_bot):
        return
    if not in_group and not from_owner:
        return

    text = _strip_mention(message.text, me.username or "")
    user_id = message.from_user.id if message.from_user else None

    log.info("Pipeline triggered by user=%s text=%r", user_id, text[:60])
    try:
        result = await run(text, user_id)
        await reply(message, result)
    except Exception as exc:
        log.exception("Pipeline error: %s", exc)
        await reply(message, "Произошла ошибка при обработке запроса.")
