import logging

from telethon import TelegramClient, events

from app.userbot.storage import save_event

logger = logging.getLogger(__name__)


def register_handlers(client: TelegramClient) -> None:
    @client.on(events.NewMessage)
    async def on_new_message(event) -> None:
        try:
            await save_event(event)
        except Exception:
            logger.exception("Userbot: failed to save new message")

    @client.on(events.MessageEdited)
    async def on_edited(event) -> None:
        try:
            await save_event(event, is_edit=True)
        except Exception:
            logger.exception("Userbot: failed to save edited message")
