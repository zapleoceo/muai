import logging

from aiogram import Bot, Router
from aiogram.types import Message

from app.bot.progress import progress
from app.config import get_settings
from app.media.extractor import extract_text
from app.orchestrator.pipeline import run
import re

from app.triage.followup import handle as handle_followup
from app.triage.pending import pop_pending

_FOLLOWUP_RE = re.compile(r"Что сделать с #(\d+)")
_INLINE_EVENT_RE = re.compile(r"^\s*#(\d+)\b")  # only when message STARTS with #N
_PROPOSAL_RE = re.compile(r"#proposal-(\d+)\b", re.IGNORECASE)
_TOKEN_RE = re.compile(r"#token-([\w.-]+)\s+(\w+)\s+(.+)", re.IGNORECASE | re.DOTALL)

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

            replied_text = ""
            replied_to_bot = (message.reply_to_message
                              and message.reply_to_message.from_user
                              and message.reply_to_message.from_user.id == me.id)
            if replied_to_bot:
                replied_text = message.reply_to_message.text or message.reply_to_message.caption or ""

            if is_dm:
                m = _PROPOSAL_RE.search(text) or _PROPOSAL_RE.search(replied_text)
                if m:
                    from app.self_extend.proposer import handle_followup as se_followup
                    pid = int(m.group(1))
                    cleaned = _PROPOSAL_RE.sub("", text).strip() or text
                    reply = await se_followup(pid, cleaned)
                    await p.finish(reply)
                    return
                m = _TOKEN_RE.search(text) or _TOKEN_RE.search(replied_text)
                if m:
                    from app.self_extend.token_watcher import apply_token_update
                    reply = await apply_token_update(m.group(1), m.group(2), m.group(3).strip())
                    await p.finish(reply)
                    return

            followup_event_id: int | None = None
            # Priority 1: short-lived pending state set by "Свой ответ" click.
            if is_dm and from_owner and user_id:
                pending = await pop_pending(user_id)
                if pending is not None:
                    followup_event_id = pending
            # Priority 2: explicit reply to the bot's "Что сделать с #N?" prompt.
            if followup_event_id is None and replied_to_bot:
                m = _FOLLOWUP_RE.search(replied_text)
                if m:
                    followup_event_id = int(m.group(1))
            if followup_event_id is None and is_dm:
                m = _INLINE_EVENT_RE.search(text)
                if m:
                    followup_event_id = int(m.group(1))
                    text = _INLINE_EVENT_RE.sub("", text).strip() or text
            if followup_event_id is not None:
                reply = await handle_followup(followup_event_id, text)
                await p.finish(reply)
                return

            reply, trace_footer = await run(text, user_id, progress_cb=p.update)
            # Persist Dima's free-text DM instruction to the brain so future
            # triages of unrelated events can still surface it via retrieval.
            if from_owner and user_id and len(text) >= 6:
                try:
                    from app.graph import write as gw
                    gw.write_instruction(user_id, text)
                except Exception as exc:
                    log.warning("write_instruction failed: %s", exc)
            await p.finish(reply)
            if trace_footer:
                try:
                    await message.answer(trace_footer.lstrip("\n"))
                except Exception as exc:
                    log.warning("Failed to send trace footer: %s", exc)
        except Exception as exc:
            log.exception("Pipeline error: %s", exc)
            await p.finish("⚠️ Произошла ошибка при обработке запроса.")
