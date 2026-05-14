import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from telethon import TelegramClient

from app.db.database import AsyncSessionLocal
from app.db.models import ChatSyncConfig
from app.db.repository import MessageRepo
from app.services.chat_settings import (
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

_CHECK_CANCEL_EVERY = 50


def _effective_since(cfg: ChatSyncConfig | None, depth_days: int) -> datetime:
    """Return the optimal start date for iter_messages.

    Uses last_synced_at for incremental sync unless depth increased since last run,
    in which case falls back to now - depth_days for a full re-sync.
    """
    since = datetime.now(tz=timezone.utc) - timedelta(days=depth_days)
    if cfg is None or cfg.last_synced_at is None:
        return since
    depth_increased = (cfg.synced_depth_days or 0) < depth_days
    if depth_increased:
        return since
    # Incremental: only fetch messages we haven't seen yet
    last = cfg.last_synced_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return last if last > since else since


def _can_skip(cfg: ChatSyncConfig | None, depth_days: int, dialog_date: datetime) -> bool:
    """True when no new messages and depth hasn't grown — nothing to do."""
    if cfg is None or cfg.last_synced_at is None:
        return False
    depth_increased = (cfg.synced_depth_days or 0) < depth_days
    if depth_increased:
        return False
    last = cfg.last_synced_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    if dialog_date.tzinfo is None:
        dialog_date = dialog_date.replace(tzinfo=timezone.utc)
    return last >= dialog_date


async def sync_history(client: TelegramClient, days: int = 2) -> None:
    mgr = get_sync_manager()
    mgr.mark_started()
    settings = await get_global_settings()
    default_depth = settings.get("default_depth_days", days)

    logger.info("Userbot: history sync started (default depth=%d days)", default_depth)

    chats_done = 0
    messages_total = 0
    skipped = 0
    cfg_cache: dict[int, ChatSyncConfig] = {}
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(ChatSyncConfig)
        )).scalars().all()
        cfg_cache = {int(c.chat_id): c for c in rows}

    try:
        async for dialog in client.iter_dialogs():
            ctype = chat_type(dialog.entity)
            ctitle = chat_title(dialog.entity) or str(dialog.id)
            chat_id = dialog.id

            if getattr(dialog.entity, "deleted", False):
                async with AsyncSessionLocal() as session:
                    await MessageRepo(session).upsert_chat_raw(
                        id=chat_id, type="deleted", title="[Удалён]", username=None,
                    )
                    await session.commit()
                logger.info("Sync: skip %d (deleted account)", chat_id)
                continue

            if not await type_allowed(ctype, settings):
                logger.debug("Sync: skip %s (type=%s not allowed)", ctitle, ctype)
                continue

            if await is_blacklisted(chat_id, getattr(dialog.entity, "username", None), settings):
                logger.info("Sync: skip %s (blacklisted)", ctitle)
                continue

            cfg = cfg_cache.get(int(chat_id))
            if cfg is None:
                # upsert chat first so FK constraint is satisfied
                async with AsyncSessionLocal() as session:
                    await MessageRepo(session).upsert_chat_raw(
                        id=chat_id,
                        type=ctype,
                        title=ctitle,
                        username=getattr(dialog.entity, "username", None),
                    )
                    await session.commit()
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

            mgr.update_progress(ctitle, chat_id, chats_done, messages_total)

            if _can_skip(cfg, chat_depth, dialog.date):
                logger.debug("Sync: skip %s (up to date)", ctitle)
                skipped += 1
                continue

            effective = _effective_since(cfg, chat_depth)
            saved = await _sync_entity(client, dialog.entity, chat_depth, chat_id, ctitle, since=effective)
            chats_done += 1
            messages_total += saved
            mgr.update_progress(ctitle, chat_id, chats_done, messages_total)

            if saved:
                logger.info("  %s: +%d messages", ctitle, saved)

    except asyncio.CancelledError:
        logger.info("Userbot: sync task was cancelled")
    except Exception:
        logger.exception("Userbot: unexpected error during sync")
    finally:
        mgr.mark_done()
        logger.info(
            "Userbot: history sync done — %d synced, %d skipped (up to date), %d messages",
            chats_done, skipped, messages_total,
        )


async def _sync_entity(
    client: TelegramClient,
    entity,
    depth_days: int,
    chat_id: int,
    ctitle: str,
    *,
    since: datetime | None = None,
) -> int:
    mgr = get_sync_manager()
    if since is None:
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

        now = datetime.now(tz=timezone.utc)
        async with AsyncSessionLocal() as session:
            await session.execute(
                insert(ChatSyncConfig)
                .values(chat_id=chat_id, last_synced_at=now, synced_depth_days=depth_days)
                .on_conflict_do_update(
                    index_elements=["chat_id"],
                    set_={"last_synced_at": now, "synced_depth_days": depth_days},
                )
            )
            await session.commit()

    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Userbot: failed to sync %s", ctitle)

    return saved


async def sync_single_chat(chat_id: int) -> int:
    client = get_client()
    if not client.is_connected():
        raise RuntimeError("Userbot not connected")

    settings = await get_global_settings()
    default_depth = settings.get("default_depth_days", 7)
    cfg = await get_chat_config(chat_id)
    depth = cfg.depth_days if cfg and cfg.depth_days is not None else default_depth

    entity = await client.get_entity(chat_id)
    ctitle = chat_title(entity) or str(chat_id)
    since = _effective_since(cfg, depth)

    logger.info("sync_single_chat: start %s (since %s)", ctitle, since.date())
    saved = await _sync_entity(client, entity, depth, chat_id, ctitle, since=since)
    logger.info("sync_single_chat: done %s → +%d messages", ctitle, saved)
    return saved
