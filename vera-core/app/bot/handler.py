import logging

from aiogram import Bot, Router
from aiogram.types import Message

from app.bot.progress import progress
from app.config import get_settings
from app.media.extractor import extract_text
from app.orchestrator.pipeline import run

log = logging.getLogger(__name__)
router = Router()


def _is_mention(message: Message, bot_username: str) -> bool:
    text = message.text or message.caption
    entities = message.entities or message.caption_entities
    if not entities or not text:
        return False
    for entity in entities:
        if entity.type == "mention":
            mention = text[entity.offset : entity.offset + entity.length]
            if mention.lower() == f"@{bot_username.lower()}":
                return True
    return False


def _strip_mention(text: str | None, bot_username: str) -> str:
    if not text:
        return ""
    prefix = f"@{bot_username}"
    stripped = text.strip()
    if stripped.lower().startswith(prefix.lower()):
        stripped = stripped[len(prefix):].strip()
    return stripped


def _has_payload(message: Message) -> bool:
    return any([
        message.text, message.voice, message.audio, message.photo,
        message.document, message.video, message.video_note,
    ])


@router.message()
async def handle_message(message: Message, bot: Bot) -> None:
    if not _has_payload(message):
        return

    settings = get_settings()
    me = await bot.get_me()
    is_dm = message.chat.type == "private"
    from_owner = message.from_user and message.from_user.id == settings.owner_telegram_id

    mentioned = _is_mention(message, me.username or "")
    replied_to_bot = (
        message.reply_to_message is not None
        and message.reply_to_message.from_user is not None
        and message.reply_to_message.from_user.id == me.id
    )

    if is_dm:
        if not from_owner:
            return
    else:
        # In groups: only the configured one, and only when addressed.
        if message.chat.id != settings.vera_group_id:
            return
        if not (mentioned or replied_to_bot):
            return

    user_id = message.from_user.id if message.from_user else None
    log.info("Pipeline triggered by user=%s media=%s", user_id,
             [k for k in ("text","voice","audio","photo","document","video","video_note")
              if getattr(message, k, None)])

    async with progress(bot, message, "🤔 Думаю...") as p:
        try:
            text = await extract_text(bot, message, p.update)
            if text is None:
                await p.finish("⚠️ Не понял сообщение.")
                return

            text = _strip_mention(text, me.username or "")
            if not text:
                await p.finish("⚠️ Пустое сообщение.")
                return

            result = await run(text, user_id, progress_cb=p.update)
            await p.finish(result)
        except Exception as exc:
            log.exception("Pipeline error: %s", exc)
            await p.finish("⚠️ Произошла ошибка при обработке запроса.")
