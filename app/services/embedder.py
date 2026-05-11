import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import text

from app.db.database import AsyncSessionLocal
from app.db.repository import MessageRepo
from app.llm.embedding import embed_text

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 5      # messages per chunk
_CHUNK_STEP = 3      # step (overlap = 2)
_MIN_CHARS = 30
_BATCH_DELAY = 0.5   # seconds between API calls

# ── status singleton ──────────────────────────────────────────────────────────

@dataclass
class EmbedderStatus:
    running: bool = False
    current_chat: str = ""
    chats_done: int = 0
    chunks_added: int = 0
    total_chunks: int = 0
    last_run: datetime | None = None
    errors: list[str] = field(default_factory=list)


_status = EmbedderStatus()


def get_embedder_status() -> dict:
    return {
        "running": _status.running,
        "current_chat": _status.current_chat,
        "chats_done": _status.chats_done,
        "chunks_added": _status.chunks_added,
        "total_chunks": _status.total_chunks,
        "last_run": _status.last_run.isoformat() if _status.last_run else None,
        "last_errors": _status.errors[-5:],
    }


# ── chunk formatting ──────────────────────────────────────────────────────────

def _speaker(msg, user, chat_type: str) -> str:
    if msg.direction == "out":
        return "Я"
    if user:
        parts = [p for p in [user.first_name, user.last_name] if p]
        name = " ".join(parts) if parts else user.username or "Собеседник"
        if user.is_bot:
            name += " [бот]"
        return name
    return "Собеседник"


def _tg_link(chat_id: int, username: str | None, tg_msg_id: int | None) -> str | None:
    if not tg_msg_id:
        return None
    if username:
        return f"https://t.me/{username}/{tg_msg_id}"
    s = str(chat_id)
    if s.startswith("-100"):
        return f"https://t.me/c/{s[4:]}/{tg_msg_id}"
    return None


def _format_chunk(rows: list, chat_title: str, chat_type: str,
                  chat_id: int = 0, chat_username: str | None = None) -> str:
    date = rows[0][0].date_utc
    type_label = {
        "private": "личный", "group": "группа",
        "supergroup": "супергруппа", "channel": "канал",
    }.get(chat_type, chat_type)
    first_tg_id = next((r[0].telegram_msg_id for r in rows if r[0].telegram_msg_id), None)
    link = _tg_link(chat_id, chat_username, first_tg_id)
    link_part = f" | {link}" if link else ""
    header = f"[Чат: {chat_title} | {type_label} | {date.strftime('%Y-%m-%d') if date else ''}{link_part}]"
    lines = [header]
    for msg, user in rows:
        speaker = _speaker(msg, user, chat_type)
        text_content = msg.text or msg.caption
        if not text_content:
            text_content = f"[{msg.media_type or 'медиа'}]"
        lines.append(f"{speaker}: {text_content}")
    return "\n".join(lines)


# ── per-chat embedding ────────────────────────────────────────────────────────

async def embed_chat(chat_id: int, chat_title: str, chat_type: str,
                     chat_username: str | None = None) -> int:
    """Chunk and embed new messages for one chat. Returns new chunk count."""
    async with AsyncSessionLocal() as session:
        last_id = await MessageRepo(session).get_last_embedded_msg_id(chat_id)
        rows = await MessageRepo(session).get_messages_after_with_users(chat_id, after_id=last_id)

    rows = [r for r in rows if r[0].text or r[0].caption]
    if not rows:
        return 0

    chunks_saved = 0
    for i in range(0, len(rows), _CHUNK_STEP):
        window = rows[i: i + _CHUNK_SIZE]
        chunk_text = _format_chunk(window, chat_title, chat_type, chat_id, chat_username)
        if len(chunk_text) < _MIN_CHARS:
            continue

        date_from = window[0][0].date_utc
        date_to = window[-1][0].date_utc
        max_msg_id = max(r[0].id for r in window)
        tg_ids = [r[0].telegram_msg_id for r in window if r[0].telegram_msg_id]
        min_tg = min(tg_ids) if tg_ids else None
        max_tg = max(tg_ids) if tg_ids else None

        try:
            vector = await embed_text(chunk_text)
        except RuntimeError as exc:
            logger.warning("Embed failed chat=%d chunk=%d: %s", chat_id, i, exc)
            ts = datetime.now().strftime("%H:%M:%S")
            _status.errors.append(f"[{ts}] {chat_title}: {exc}")
            await asyncio.sleep(5)
            continue

        vec_str = "[" + ",".join(str(x) for x in vector) + "]"
        async with AsyncSessionLocal() as session:
            await session.execute(
                text(
                    "INSERT INTO message_chunks "
                    "(chat_id, chat_title, chunk_text, embedding, msg_date_from, msg_date_to, "
                    "max_msg_id, min_tg_msg_id, max_tg_msg_id, chat_username) "
                    "VALUES (:cid, :title, :chunk, CAST(:emb AS vector), :df, :dt, "
                    ":mid, :min_tg, :max_tg, :uname)"
                ),
                {"cid": chat_id, "title": chat_title, "chunk": chunk_text,
                 "emb": vec_str, "df": date_from, "dt": date_to, "mid": max_msg_id,
                 "min_tg": min_tg, "max_tg": max_tg, "uname": chat_username},
            )
            await session.commit()

        chunks_saved += 1
        _status.chunks_added += 1
        await asyncio.sleep(_BATCH_DELAY)

    return chunks_saved


# ── full pass ─────────────────────────────────────────────────────────────────

async def embed_all_chats() -> None:
    _status.running = True
    _status.chats_done = 0
    _status.errors = []
    logger.info("Embedder: starting pass")

    async with AsyncSessionLocal() as session:
        chats = await MessageRepo(session).list_all_chats()

    for chat in chats:
        _status.current_chat = chat.title or str(chat.id)
        try:
            n = await embed_chat(chat.id, chat.title or str(chat.id), chat.type or "unknown", chat.username)
            if n:
                logger.info("Embedder: %s → +%d chunks", chat.title, n)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Embedder: failed for chat %s", chat.title)
            ts = datetime.now().strftime("%H:%M:%S")
            _status.errors.append(f"[{ts}] {chat.title}: {exc}")
        _status.chats_done += 1

    async with AsyncSessionLocal() as session:
        stats = await MessageRepo(session).chunk_stats()
    _status.total_chunks = stats["total_chunks"]
    _status.running = False
    _status.current_chat = ""
    _status.last_run = datetime.now()
    logger.info("Embedder: pass done — %d total chunks", _status.total_chunks)


async def run_embedder_loop() -> None:
    """30s boot delay, then embed all, repeat every hour."""
    async with AsyncSessionLocal() as session:
        stats = await MessageRepo(session).chunk_stats()
    _status.total_chunks = stats["total_chunks"]

    await asyncio.sleep(30)
    while True:
        try:
            await embed_all_chats()
        except asyncio.CancelledError:
            _status.running = False
            break
        except Exception:
            logger.exception("Embedder loop error")
            _status.running = False
        await asyncio.sleep(3600)
