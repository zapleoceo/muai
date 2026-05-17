import asyncio
import logging

from telethon import TelegramClient, events
from telethon.tl.types import MessageEntityMention, MessageEntityMentionName

from app.services.message_ingest import ingest_telethon_event
from app.userbot.media import chat_title

logger = logging.getLogger(__name__)


def _is_owner_mentioned(msg, owner_id: int | None, owner_username: str | None) -> bool:
    for ent in (msg.entities or []):
        if isinstance(ent, MessageEntityMentionName):
            if owner_id and ent.user_id == owner_id:
                return True
        elif isinstance(ent, MessageEntityMention) and owner_username and msg.text:
            fragment = msg.text[ent.offset:ent.offset + ent.length]
            if fragment.lstrip("@").lower() == owner_username:
                return True
    return False


def register_handlers(client: TelegramClient) -> None:
    @client.on(events.NewMessage)
    async def on_new_message(event) -> None:
        try:
            await ingest_telethon_event(event)
        except Exception:
            logger.exception("Userbot: failed to save new message")
            return

        # Immediate live embedding for new messages
        try:
            from app.services.live_embedder import embed_chat_live
            asyncio.create_task(embed_chat_live(event.chat_id))
        except Exception:
            pass

        # Check for owner mention in incoming messages
        msg = event.message
        if msg.out:
            return  # owner's own messages — skip mention check

        try:
            from app.userbot.client import get_owner_info
            owner_id, owner_username = get_owner_info()

            if _is_owner_mentioned(msg, owner_id, owner_username):
                chat = await event.get_chat()
                sender = await event.get_sender()
                sender_name = "Unknown"
                if sender:
                    parts = [p for p in [getattr(sender, "first_name", None), getattr(sender, "last_name", None)] if p]
                    sender_name = " ".join(parts) or getattr(sender, "username", None) or "Unknown"

                text = msg.text or msg.message or ""
                if text:
                    from app.services.mention_alert import notify_owner_mention
                    asyncio.create_task(notify_owner_mention(
                        chat_id=event.chat_id,
                        chat_title=chat_title(chat),
                        sender_name=sender_name,
                        message_text=text,
                    ))
        except Exception:
            logger.exception("Userbot: failed to process mention alert")

    @client.on(events.MessageEdited)
    async def on_edited(event) -> None:
        try:
            await ingest_telethon_event(event, is_edit=True)
        except Exception:
            logger.exception("Userbot: failed to save edited message")
