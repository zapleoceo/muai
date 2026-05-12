import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.dialects.postgresql import insert
from telethon import TelegramClient

from app.db.database import AsyncSessionLocal
from app.db.models import ChatSyncConfig
from app.db.repository import MessageRepo
from app.services.chat_settings import (
    auto_approve_existing_chats,
    create_pending,
    get_chat_config,
    get_global_settings,
    is_blacklisted,
    type_allowed,
)
from app.services.sync_manager import get_sync_manager
from app.userbot.client import get_client
from app.userbot.media import chat_title, chat_type, chat_username
from app.userbot.storage import save_history_message

logger = logging.getLogger(__name__)

_CHECK_CANCEL_EVERY = 50   # check cancellation flag every N messages


async def sync_history(client: TelegramClient, days: int = 2) -> None:
    mgr = get_sync_manager()
    mgr.mark_started()
    settings = await get_global_settings()
    default_depth = settings.get("default_depth_days", days)

    logger.info("Userbot: history sync started (default depth=%d days)", default_depth)

    chats_done = 0
    messages_total = 0

    try:
        async for dialog in client.iter_dialogs():
            ctype = chat_type(dialog.entity)
            ctitle = chat_title(dialog.entity) or str(dialog.id)
            chat_id = dialog.id

            # refresh settings each dialog so live changes take effect
            settings = await get_global_settings()

            if not await type_allowed(ctype, settings):
                logger.debug("Sync: skip %s (type=%s not allowed)", ctitle, ctype)
                continue

            if await is_blacklisted(chat_id, getattr(dialog.entity, "username", None), settings):
                logger.info("Sync: skip %s (blacklisted)", ctitle)
                continue

            cfg = await get_chat_config(chat_id)
            if cfg is None:
                # new chat — register as pending, skip sync
                await create_pending(chat_id)
                logger.info("Sync: new chat queued as pending: %s", ctitle)
                continue

            if not cfg.enabled:
                logger.debug("Sync: skip %s (not enabled)", ctitle)
                continue

            if mgr.is_cancelled(chat_id):
                mgr.clear_cancel(chat_id)
                logger.info("Sync: skip %s (cancelled)", ctitle)
                continue

            chat_depth = cfg.depth_days if cfg.depth_days is not None else default_depth
            saved = await _sync_dialog(client, dialog, chat_depth, chat_id, ctitle)
            chats_done += 1
            messages_total += saved
            mgr.update_progress(ctitle, chats_done, messages_total)

            if saved:
                logger.info("  %s: +%d messages", ctitle, saved)

    except asyncio.CancelledError:
        logger.info("Userbot: sync task was cancelled")
    except Exception:
        logger.exception("Userbot: unexpected error during sync")
    finally:
        mgr.mark_done()
        logger.info("Userbot: history sync done — %d chats, %d messages", chats_done, messages_total)


async def _sync_dialog(
    client: TelegramClient,
    dialog,
    depth_days: int,
    chat_id: int,
    ctitle: str,
) -> int:
    return await _sync_entity(client, dialog.entity, depth_days, chat_id, ctitle)


async def _sync_entity(
    client: TelegramClient,
    entity,
    depth_days: int,
    chat_id: int,
    ctitle: str,
) -> int:
    mgr = get_sync_manager()
    since = datetime.now(tz=timezone.utc) - timedelta(days=depth_days)
    saved = 0
    user_cache: dict[int, bool] = {}

    try:
        async with AsyncSessionLocal() as session:
            await MessageRepo(session).upsert_chat_raw(
                id=chat_id,
                type=chat_type(entity),
                title=ctitle,
                username=chat_username(entity),
            )
            await session.commit()

        counter = 0
        async for msg in client.iter_messages(entity, reverse=True, offset_date=since, limit=None):
            if not msg.text and not msg.media:
                continue
            counter += 1
            if counter % _CHECK_CANCEL_EVERY == 0:
                if mgr.is_cancelled(chat_id):
                    mgr.clear_cancel(chat_id)
                    logger.info("Sync: mid-loop cancel for %s at msg %d", ctitle, counter)
                    break
            if await save_history_message(
                chat_id=chat_id,
                chat_entity=entity,
                msg=msg,
                user_cache=user_cache,
            ):
                saved += 1

        async with AsyncSessionLocal() as session:
            await session.execute(
                insert(ChatSyncConfig)
                .values(chat_id=chat_id, last_synced_at=datetime.now(tz=timezone.utc))
                .on_conflict_do_update(
                    index_elements=["chat_id"],
                    set_={"last_synced_at": datetime.now(tz=timezone.utc)},
                )
            )
            await session.commit()

    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Userbot: failed to sync %s", ctitle)

    return saved


async def sync_single_chat(chat_id: int) -> int:
    from app.services.chat_settings import get_chat_config, get_global_settings
    client = get_client()
    if not client.is_connected():
        raise RuntimeError("Userbot not connected")

    settings = await get_global_settings()
    default_depth = settings.get("default_depth_days", 7)
    cfg = await get_chat_config(chat_id)
    depth = cfg.depth_days if cfg and cfg.depth_days is not None else default_depth

    entity = await client.get_entity(chat_id)
    ctitle = chat_title(entity) or str(chat_id)
    saved = await _sync_entity(client, entity, depth, chat_id, ctitle)
    logger.info("sync_single_chat: %s → +%d messages", ctitle, saved)
    return saved
