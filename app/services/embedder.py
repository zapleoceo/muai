import asyncio
import logging

from sqlalchemy import text

from app.db.database import AsyncSessionLocal
from app.db.repository import MessageRepo
from app.llm.embedding import embed_text

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 5      # messages per chunk
_CHUNK_STEP = 3      # step (overlap = CHUNK_SIZE - CHUNK_STEP = 2)
_MIN_CHARS = 30      # skip chunks shorter than this
_BATCH_DELAY = 0.5   # seconds between API calls


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
    """Chunk and embed messages for one chat not yet embedded. Returns new chunk count."""
    async with AsyncSessionLocal() as session:
        repo = MessageRepo(session)
        last_embedded_msg_id = await repo.get_last_embedded_msg_id(chat_id)
        rows = await repo.get_messages_after(chat_id, after_id=last_embedded_msg_id)

    if not rows:
        return 0

    # We need user info — fetch with user join
    msg_ids = {r.id for r in rows}
    async with AsyncSessionLocal() as session:
        all_rows = await MessageRepo(session).get_recent_messages_with_users(chat_id, limit=10_000)
    rows_with_users = [r for r in all_rows if r[0].id in msg_ids]

    if not rows_with_users:
        return 0

    chunks_saved = 0
    last_saved_msg_id: int | None = None

    for i in range(0, len(rows_with_users), _CHUNK_STEP):
        window = rows_with_users[i: i + _CHUNK_SIZE]
        chunk_text = _format_chunk(window, chat_title)
        if len(chunk_text) < _MIN_CHARS:
            continue

        date_from = window[0][0].date_utc
        date_to = window[-1][0].date_utc
        max_msg_id = max(r[0].id for r in window)

        try:
            vector = await embed_text(chunk_text)
        except RuntimeError as exc:
            logger.warning("Embedding failed for chat %d chunk %d: %s", chat_id, i, exc)
            await asyncio.sleep(5)
            continue

        vec_str = "[" + ",".join(str(x) for x in vector) + "]"
        async with AsyncSessionLocal() as session:
            await session.execute(
                text(
                    "INSERT INTO message_chunks "
                    "(chat_id, chat_title, chunk_text, embedding, msg_date_from, msg_date_to) "
                    "VALUES (:chat_id, :title, :chunk, CAST(:emb AS vector), :df, :dt)"
                ),
                {"chat_id": chat_id, "title": chat_title, "chunk": chunk_text,
                 "emb": vec_str, "df": date_from, "dt": date_to},
            )
            await session.commit()

        last_saved_msg_id = max_msg_id
        chunks_saved += 1
        await asyncio.sleep(_BATCH_DELAY)

    return chunks_saved


async def embed_all_chats() -> None:
    logger.info("Embedder: starting embedding pass")
    async with AsyncSessionLocal() as session:
        chats = await MessageRepo(session).list_all_chats()

    total = 0
    for chat in chats:
        try:
            n = await embed_chat(chat.id, chat.title or str(chat.id))
            if n:
                logger.info("Embedder: %s → +%d chunks", chat.title, n)
                total += n
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Embedder: failed for chat %s", chat.title)

    logger.info("Embedder: pass done — %d new chunks total", total)


async def run_embedder_loop() -> None:
    """Start embedding 30s after boot, then re-run every hour to pick up new messages."""
    await asyncio.sleep(30)
    while True:
        try:
            await embed_all_chats()
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Embedder loop error")
        await asyncio.sleep(3600)
