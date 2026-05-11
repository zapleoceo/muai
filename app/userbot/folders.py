import logging

from sqlalchemy import select
from telethon.tl.functions.messages import GetDialogFiltersRequest
from telethon.tl.types import DialogFilter, InputPeerChannel, InputPeerChat, InputPeerUser

from app.db.database import AsyncSessionLocal
from app.db.models import Chat
from app.userbot.client import get_client

logger = logging.getLogger(__name__)


def _peer_to_db_id(peer) -> int | None:
    """Convert Telethon InputPeer to the chat ID as stored in our DB."""
    if isinstance(peer, InputPeerUser):
        return peer.user_id
    if isinstance(peer, InputPeerChat):
        return -peer.chat_id
    if isinstance(peer, InputPeerChannel):
        return int(f"-100{peer.channel_id}")
    return None


async def sync_folders() -> int:
    client = get_client()
    if not client.is_connected():
        raise RuntimeError("Userbot not connected")

    result = await client(GetDialogFiltersRequest())
    filters = [f for f in result.filters if isinstance(f, DialogFilter)]

    folder_map: dict[int, str] = {}
    for f in filters:
        for peer in (f.include_peers or []):
            db_id = _peer_to_db_id(peer)
            if db_id is not None and db_id not in folder_map:
                folder_map[db_id] = f.title

    async with AsyncSessionLocal() as session:
        chats = (await session.execute(select(Chat))).scalars().all()
        updated = 0
        for chat in chats:
            new_folder = folder_map.get(chat.id)
            if chat.folder != new_folder:
                chat.folder = new_folder
                updated += 1
        await session.commit()

    logger.info("sync_folders: updated %d chats", updated)
    return updated
