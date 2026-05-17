"""
Immediate embedding for newly received messages.

Designed to run after every live message arrives via the userbot.
Uses tokens with 'embed_live' capability (falls back to 'embed' if none configured).
Only embeds the latest unembedded messages of a specific chat — much faster than
the batch embedder which processes all chats.
"""
import logging
from datetime import timezone
from sqlalchemy import select, text

from app.db.database import AsyncSessionLocal
from app.db.models import Message
from app.llm.embedding import embed_texts

logger = logging.getLogger(__name__)

_MIN_CHARS = 20
_LIVE_WINDOW = 12        # max messages to embed in one live pass
_CAPABILITY = "embed_live"

_INSERT_SQL = text(
    "INSERT INTO message_chunks "
    "(chat_id, chat_title, chunk_text, embedding, msg_date_from, msg_date_to, "
    "min_msg_id, max_msg_id, msg_count, min_tg_msg_id, max_tg_msg_id, meta) "
    "VALUES (:cid, :title, :chunk, CAST(:emb AS vector), :df, :dt, "
    ":min_mid, :max_mid, :cnt, :min_tg, :max_tg, CAST(:meta AS jsonb)) "
    "ON CONFLICT DO NOTHING"
)


async def embed_chat_live(chat_id: int) -> None:
    """Embed the most recent unembedded messages for one chat using embed_live tokens."""
    try:
        await _run(chat_id)
    except Exception:
        logger.exception("live_embedder: failed for chat_id=%s", chat_id)


async def _get_last_embedded_max_msg_id(chat_id: int) -> int:
    async with AsyncSessionLocal() as session:
        row = await session.execute(
            text("SELECT COALESCE(MAX(max_msg_id), 0) FROM message_chunks WHERE chat_id = :cid"),
            {"cid": chat_id},
        )
        return int(row.scalar() or 0)


async def _run(chat_id: int) -> None:
    last_embedded_id = await _get_last_embedded_max_msg_id(chat_id)

    async with AsyncSessionLocal() as session:
        # Get chat info
        chat_row = await session.execute(
            text("SELECT title, type, username FROM chats WHERE id = :cid"),
            {"cid": chat_id},
        )
        chat_info = chat_row.fetchone()
        if not chat_info:
            return
        chat_title, chat_type, chat_username = chat_info

        # Get recent unembedded messages
        rows = (await session.execute(
            select(Message)
            .where(
                Message.chat_id == chat_id,
                Message.id > last_embedded_id,
                (Message.text.isnot(None)) | (Message.caption.isnot(None)),
            )
            .order_by(Message.id)
            .limit(_LIVE_WINDOW)
        )).scalars().all()

    if not rows:
        return

    # Build chunk text
    type_label = {"private": "личный", "group": "группа",
                  "supergroup": "супергруппа", "channel": "канал"}.get(chat_type or "", "")
    first_date = rows[0].date_utc
    header = f"[Чат: {chat_title} | {type_label} | {first_date.strftime('%Y-%m-%d') if first_date else ''}]"
    lines = [header]
    for msg in rows:
        speaker = "Я" if msg.direction == "out" else "Собеседник"
        content = msg.text or msg.caption or f"[{msg.media_type or 'медиа'}]"
        lines.append(f"{speaker}: {content}")
    chunk_text = "\n".join(lines)

    if len(chunk_text) < _MIN_CHARS:
        return

    vectors = await embed_texts([chunk_text], capability=_CAPABILITY)
    if not vectors or not vectors[0]:
        return

    msg_ids = [int(m.id) for m in rows if m.id]
    tg_ids = [int(m.telegram_msg_id) for m in rows if m.telegram_msg_id]
    import json
    params = {
        "cid": chat_id,
        "title": chat_title,
        "chunk": chunk_text,
        "emb": json.dumps(vectors[0]),
        "df": rows[0].date_utc.replace(tzinfo=timezone.utc) if rows[0].date_utc else None,
        "dt": rows[-1].date_utc.replace(tzinfo=timezone.utc) if rows[-1].date_utc else None,
        "min_mid": min(msg_ids),
        "max_mid": max(msg_ids),
        "cnt": len(rows),
        "min_tg": min(tg_ids) if tg_ids else None,
        "max_tg": max(tg_ids) if tg_ids else None,
        "meta": json.dumps({"live": True}),
    }

    async with AsyncSessionLocal() as session:
        await session.execute(_INSERT_SQL, params)
        await session.commit()

    logger.debug("live_embedder: chat_id=%s embedded %d messages", chat_id, len(rows))
