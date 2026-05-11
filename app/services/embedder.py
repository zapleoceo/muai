import asyncio
import logging
from datetime import datetime

from app.db.database import AsyncSessionLocal
from app.db.models import MessageChunk
from app.db.repository import MessageRepo
from app.llm.embedding import embed_text

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 5      # messages per chunk
_CHUNK_STEP = 3      # step between chunk starts (overlap = CHUNK_SIZE - CHUNK_STEP)
_MIN_CHARS = 30      # skip chunks shorter than this
_BATCH_DELAY = 0.5   # seconds between embedding API calls


def _format_chunk(rows: list, chat_title: str) -> str:
    date = rows[0][0].date_utc
    header = f"[{chat_title} | {date.strftime('%Y-%m-%d') if date else ''}]"
    lines = [header]
    for msg, user in rows:
        if msg.direction == "out":
            speaker = "Я"
        elif user:
            speaker = user.first_name or user.username or "Собеседник"
        else:
            speaker = "Собеседник"
        text = msg.text or msg.caption or f"[{msg.media_type or 'медиа'}]"
        lines.append(f"{speaker}: {text}")
    return "\n".join(lines)


async def embed_chat(chat_id: int, chat_title: str) -> int:
    """Chunk and embed all messages for one chat. Returns number of new chunks created."""
    async with AsyncSessionLocal() as session:
        repo = MessageRepo(session)
        rows = await repo.get_recent_messages_with_users(chat_id, limit=10_000)
        existing_dates = await repo.get_embedded_date_range(chat_id)

    if not rows:
        return 0

    # Filter to only messages not yet covered by existing chunks
    if existing_dates:
        newest_embedded = existing_dates
        rows = [r for r in rows if r[0].date_utc and r[0].date_utc > newest_embedded]

    if not rows:
        return 0

    chunks_saved = 0
    for i in range(0, len(rows), _CHUNK_STEP):
        window = rows[i: i + _CHUNK_SIZE]
        chunk_text = _format_chunk(window, chat_title)
        if len(chunk_text) < _MIN_CHARS:
            continue

        date_from = window[0][0].date_utc
        date_to = window[-1][0].date_utc

        try:
            vector = await embed_text(chunk_text)
        except RuntimeError as exc:
            logger.warning("Embedding failed for chat %d chunk %d: %s", chat_id, i, exc)
            await asyncio.sleep(5)
            continue

        chunk = MessageChunk(
            chat_id=chat_id,
            chat_title=chat_title,
            chunk_text=chunk_text,
            embedding=vector,
            msg_date_from=date_from,
            msg_date_to=date_to,
        )
        async with AsyncSessionLocal() as session:
            session.add(chunk)
            await session.commit()

        chunks_saved += 1
        await asyncio.sleep(_BATCH_DELAY)

    return chunks_saved


async def embed_all_chats() -> None:
    """Background task: embed all approved chats that have un-embedded messages."""
    logger.info("Embedder: starting full embedding pass")
    async with AsyncSessionLocal() as session:
        chats = await MessageRepo(session).list_all_chats()

    total = 0
    for chat in chats:
        try:
            n = await embed_chat(chat.id, chat.title or str(chat.id))
            if n:
                logger.info("Embedder: %s → %d new chunks", chat.title, n)
                total += n
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Embedder: failed for chat %s", chat.title)

    logger.info("Embedder: done — %d total new chunks", total)


async def run_embedder_loop() -> None:
    """Long-running background task. Embeds once on start, then checks every hour."""
    await asyncio.sleep(30)  # wait for sync to start first
    while True:
        try:
            await embed_all_chats()
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Embedder loop error")
        await asyncio.sleep(3600)  # re-embed new messages every hour
