import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from app.db.database import AsyncSessionLocal
from app.db.models import Chat, ChatSyncConfig
from app.db.repository import MessageRepo
from app.llm.embedding import embed_texts
from app.services.chat_sync_settings_service import ChatSyncSettingsService

logger = logging.getLogger(__name__)

_MIN_CHARS = 30
_BATCH_DELAY = 0.3        # seconds between API calls (Voyage has no per-request limit)
_INSERT_BATCH = 25
_EMBED_BATCH = 10
_PAGE_SIZE = 800
_SESSION_GAP = timedelta(minutes=20)
_MAX_CHUNK_CHARS = 4800   # larger chunks → fewer API calls
_MAX_CHUNK_MSGS = 60
_MIN_CHAT_MESSAGES = 50   # skip tiny chats — not worth embedding (noise)

# ── status singleton ──────────────────────────────────────────────────────────

_DEFAULT_CHAT_TYPES = ["private", "group"]

@dataclass
class EmbedderStatus:
    running: bool = False
    current_chat: str = ""
    chats_done: int = 0
    chunks_added: int = 0
    total_chunks: int = 0
    messages_pending: int = 0
    last_run: datetime | None = None
    errors: list[str] = field(default_factory=list)
    enabled: bool = True
    chat_types: list[str] = field(default_factory=lambda: list(_DEFAULT_CHAT_TYPES))


_status = EmbedderStatus()


def get_embedder_status() -> dict:
    return {
        "running": _status.running,
        "enabled": _status.enabled,
        "current_chat": _status.current_chat,
        "chats_done": _status.chats_done,
        "chunks_added": _status.chunks_added,
        "total_chunks": _status.total_chunks,
        "messages_pending": _status.messages_pending,
        "last_run": _status.last_run.isoformat() if _status.last_run else None,
        "last_errors": _status.errors[-5:],
        "chat_types": list(_status.chat_types),
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


def _extract_forward_hint(raw_json: dict | None) -> str | None:
    if not raw_json:
        return None
    if raw_json.get("forward_origin"):
        return "↪ переслано"
    if raw_json.get("forward_from_chat") or raw_json.get("forward_from") or raw_json.get("forward_sender_name"):
        return "↪ переслано"
    if raw_json.get("fwd_from"):
        return "↪ переслано"
    if raw_json.get("forward_date"):
        return "↪ переслано"
    return None


def _extract_event_dates(text: str) -> list[str]:
    s = str(text or "")
    out: list[str] = []
    for m in re.finditer(r"\b\d{1,2}\.\d{1,2}(?:\.\d{2,4})?\s*-\s*\d{1,2}\.\d{1,2}(?:\.\d{2,4})?\b", s):
        out.append(m.group(0))
    for m in re.finditer(r"\b\d{1,2}\.\d{1,2}(?:\.\d{2,4})?\b", s):
        out.append(m.group(0))
    seen = set()
    uniq = []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq[:10]


def _looks_like_post(text: str) -> bool:
    s = str(text or "")
    if len(s) >= 700:
        return True
    if s.count("\n") >= 6:
        return True
    if re.search(r"\b\d{1,2}\.\d{1,2}\s*-\s*\d{1,2}\.\d{1,2}\b", s):
        return True
    if "афиша" in s.lower() or "распис" in s.lower() or "schedule" in s.lower() or "events" in s.lower():
        return True
    return False


def _format_chunk(rows: list, chat_title: str, chat_type: str,
                  chat_id: int = 0, chat_username: str | None = None) -> str:
    date = rows[0][0].date_utc
    type_label = {
        "private": "личный", "group": "группа",
        "supergroup": "супергруппа", "channel": "канал",
    }.get(chat_type, chat_type)
    header = f"[Чат: {chat_title} | {type_label} | {date.strftime('%Y-%m-%d') if date else ''}]"
    lines = [header]
    for msg, user in rows:
        speaker = _speaker(msg, user, chat_type)
        text_content = msg.text or msg.caption
        if not text_content:
            text_content = f"[{msg.media_type or 'медиа'}]"
        fwd = _extract_forward_hint(getattr(msg, "raw_json", None))
        fwd_part = f"{fwd} " if fwd else ""
        link = _tg_link(chat_id, chat_username, msg.telegram_msg_id)
        link_part = f" {link}" if link else ""
        lines.append(f"{speaker}: {fwd_part}{text_content}{link_part}")
    return "\n".join(lines)


# ── per-chat embedding ────────────────────────────────────────────────────────

async def embed_chat(chat_id: int, chat_title: str, chat_type: str,
                     chat_username: str | None = None) -> int:
    """Chunk and embed new messages for one chat. Returns new chunk count."""
    insert_sql = text(
        "INSERT INTO message_chunks "
        "(chat_id, chat_title, chunk_text, embedding, msg_date_from, msg_date_to, "
        "min_msg_id, max_msg_id, msg_count, min_tg_msg_id, max_tg_msg_id, chat_username, meta) "
        "VALUES (:cid, :title, :chunk, CAST(:emb AS vector), :df, :dt, "
        ":min_mid, :max_mid, :msg_count, :min_tg, :max_tg, :uname, CAST(:meta AS jsonb))"
    )

    chunks_saved = 0
    pending_db: list[dict] = []

    async with AsyncSessionLocal() as session:
        last_id = await MessageRepo(session).get_last_embedded_msg_id(chat_id)

    current: list = []
    current_chars = 0
    current_last_date: datetime | None = None

    def flush_current() -> dict | None:
        nonlocal current, current_chars, current_last_date
        if not current:
            return None
        chunk_text = _format_chunk(current, chat_title, chat_type, chat_id, chat_username)
        if len(chunk_text) < _MIN_CHARS:
            current = []
            current_chars = 0
            current_last_date = None
            return None

        msgs = [r[0] for r in current]
        date_from = msgs[0].date_utc
        date_to = msgs[-1].date_utc
        min_msg_id = min(int(m.id) for m in msgs if m.id is not None)
        max_msg_id = max(int(m.id) for m in msgs if m.id is not None)
        tg_ids = [int(m.telegram_msg_id) for m in msgs if m.telegram_msg_id]
        min_tg = min(tg_ids) if tg_ids else None
        max_tg = max(tg_ids) if tg_ids else None
        event_dates: list[str] = []
        for m in msgs:
            event_dates.extend(_extract_event_dates(m.text or m.caption or ""))
        meta = {
            "chat_type": chat_type,
            "has_forwards": any(bool(_extract_forward_hint(getattr(m, "raw_json", None))) for m in msgs),
            "event_dates": list(dict.fromkeys(event_dates))[:10],
        }

        out = {
            "cid": chat_id,
            "title": chat_title,
            "chunk": chunk_text,
            "df": date_from,
            "dt": date_to,
            "min_mid": min_msg_id,
            "max_mid": max_msg_id,
            "msg_count": int(len(msgs)),
            "min_tg": min_tg,
            "max_tg": max_tg,
            "uname": chat_username,
            "meta": json.dumps(meta, ensure_ascii=False),
        }
        current = []
        current_chars = 0
        current_last_date = None
        return out

    to_embed: list[dict] = []

    async with AsyncSessionLocal() as session:
        page_after = last_id
        while True:
            rows = await MessageRepo(session).get_messages_after_with_users_page(chat_id, after_id=page_after, limit=_PAGE_SIZE)
            if not rows:
                break

            for msg, user in rows:
                if not (msg.text or msg.caption):
                    continue

                txt = msg.text or msg.caption or ""
                if chat_type == "channel" or _looks_like_post(txt):
                    ready = flush_current()
                    if ready:
                        to_embed.append(ready)
                    current = [(msg, user)]
                    current_chars = len(txt)
                    current_last_date = msg.date_utc
                    ready2 = flush_current()
                    if ready2:
                        to_embed.append(ready2)
                    continue

                if current_last_date and msg.date_utc and (msg.date_utc - current_last_date) > _SESSION_GAP:
                    ready = flush_current()
                    if ready:
                        to_embed.append(ready)

                if current and (len(current) >= _MAX_CHUNK_MSGS or (current_chars + len(txt)) >= _MAX_CHUNK_CHARS):
                    ready = flush_current()
                    if ready:
                        to_embed.append(ready)

                current.append((msg, user))
                current_chars += len(txt) + 1
                current_last_date = msg.date_utc

            page_after = rows[-1][0].id if rows[-1][0].id is not None else page_after

        ready = flush_current()
        if ready:
            to_embed.append(ready)

    if not to_embed:
        return 0

    async with AsyncSessionLocal() as session:
        for i in range(0, len(to_embed), _EMBED_BATCH):
            batch = to_embed[i : i + _EMBED_BATCH]
            texts = [b["chunk"] for b in batch]
            try:
                vectors = await embed_texts(texts)
            except RuntimeError as exc:
                logger.warning("Embed failed chat=%d batch=%d: %s", chat_id, i, exc)
                ts = datetime.now(tz=timezone.utc).strftime("%H:%M:%S")
                _status.errors = _status.errors[-49:]
                _status.errors.append(f"[{ts}] {chat_title}: {exc}")
                await asyncio.sleep(5)
                continue

            for b, vec in zip(batch, vectors, strict=False):
                vec_str = "[" + ",".join(str(x) for x in vec) + "]"
                pending_db.append({**b, "emb": vec_str})

            if len(pending_db) >= _INSERT_BATCH:
                await session.execute(insert_sql, pending_db)
                await session.commit()
                chunks_saved += len(pending_db)
                _status.chunks_added += len(pending_db)
                pending_db.clear()

            await asyncio.sleep(_BATCH_DELAY)

        if pending_db:
            await session.execute(insert_sql, pending_db)
            await session.commit()
            chunks_saved += len(pending_db)
            _status.chunks_added += len(pending_db)
            pending_db.clear()

    return chunks_saved


# ── full pass ─────────────────────────────────────────────────────────────────

async def embed_all_chats() -> None:
    _status.running = True
    _status.chats_done = 0
    _status.errors = []
    logger.info("Embedder: starting pass")

    settings_svc = ChatSyncSettingsService()
    sync_settings = await settings_svc.get()

    async with AsyncSessionLocal() as session:
        pending_rows = (await session.execute(
            text(
                """
                WITH lc AS (
                    SELECT chat_id, MAX(max_msg_id) AS last_id
                    FROM message_chunks
                    GROUP BY chat_id
                )
                SELECT c.id AS chat_id, c.type AS type, c.title AS title, c.username AS username,
                       COALESCE(cfg.enabled, false) AS enabled,
                       COUNT(m.id) AS pending
                FROM chats c
                LEFT JOIN chat_sync_config cfg ON cfg.chat_id = c.id
                LEFT JOIN lc ON lc.chat_id = c.id
                JOIN messages m ON m.chat_id = c.id
                WHERE (m.text IS NOT NULL OR m.caption IS NOT NULL)
                  AND m.id > COALESCE(lc.last_id, 0)
                GROUP BY c.id, c.type, c.title, c.username, cfg.enabled
                ORDER BY pending DESC, c.title NULLS LAST, c.id ASC
                """
            )
        )).fetchall()

        chats: list[Chat] = []
        for r in pending_rows:
            chat = Chat(id=r.chat_id, type=r.type, title=r.title, username=r.username)
            chat._embed_pending = int(r.pending or 0)  # type: ignore[attr-defined]
            chat._embed_enabled = bool(r.enabled)      # type: ignore[attr-defined]
            chats.append(chat)

    allowed_types = set(_status.chat_types) if _status.chat_types else set(_DEFAULT_CHAT_TYPES)
    for chat in chats:
        if not getattr(chat, "_embed_enabled", False):
            continue
        if settings_svc.is_blacklisted(int(chat.id), getattr(chat, "username", None), sync_settings):
            continue
        if str(chat.type or "") not in allowed_types:
            continue
        # skip tiny chats — likely noise, not worth the API quota
        if getattr(chat, "_embed_pending", 0) < _MIN_CHAT_MESSAGES:
            continue

        _status.current_chat = chat.title or str(chat.id)
        try:
            n = await embed_chat(chat.id, chat.title or str(chat.id), chat.type or "unknown", chat.username)
            if n:
                logger.info("Embedder: %s → +%d chunks", chat.title, n)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Embedder: failed for chat %s", chat.title)
            ts = datetime.now(tz=timezone.utc).strftime("%H:%M:%S")
            _status.errors = _status.errors[-49:]
            _status.errors.append(f"[{ts}] {chat.title}: {exc}")
        _status.chats_done += 1

    async with AsyncSessionLocal() as session:
        stats = await MessageRepo(session).chunk_stats()
    _status.total_chunks = stats["total_chunks"]
    _status.messages_pending = stats["messages_pending"]
    _status.running = False
    _status.current_chat = ""
    _status.last_run = datetime.now(tz=timezone.utc)
    logger.info("Embedder: pass done — %d total chunks, %d pending", _status.total_chunks, _status.messages_pending)


class TextEmbedderManager:
    def __init__(self) -> None:
        self._daemon_task: asyncio.Task | None = None
        self._run_task: asyncio.Task | None = None
        self._wake = asyncio.Event()

    def start(self) -> None:
        _status.enabled = True
        self._wake.set()

    def stop(self) -> None:
        _status.enabled = False
        task = self._run_task
        if task and not task.done():
            task.cancel()

    def is_running(self) -> bool:
        task = self._run_task
        return task is not None and not task.done()

    def trigger_once(self) -> None:
        _status.enabled = True
        self._wake.set()

    def start_daemon(self) -> None:
        if self._daemon_task and not self._daemon_task.done():
            return
        self._daemon_task = asyncio.create_task(self._run_daemon())

    async def shutdown(self) -> None:
        self.stop()
        task = self._daemon_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _run_daemon(self) -> None:
        async with AsyncSessionLocal() as session:
            stats = await MessageRepo(session).chunk_stats()
        _status.total_chunks = stats["total_chunks"]
        _status.messages_pending = stats["messages_pending"]

        await asyncio.sleep(30)
        while True:
            if not _status.enabled:
                _status.running = False
                self._wake.clear()
                await self._wake.wait()
                continue

            if self._run_task and not self._run_task.done():
                self._wake.clear()
                await self._wake.wait()
                continue

            self._wake.clear()
            self._run_task = asyncio.create_task(embed_all_chats())
            try:
                await self._run_task
            except asyncio.CancelledError:
                _status.running = False
            except Exception:
                logger.exception("Embedder loop error")
                _status.running = False

            if not _status.enabled:
                continue

            try:
                await asyncio.wait_for(self._wake.wait(), timeout=3600)
            except asyncio.TimeoutError:
                pass


_text_embedder_manager: TextEmbedderManager | None = None


def get_text_embedder_manager() -> TextEmbedderManager:
    global _text_embedder_manager
    if _text_embedder_manager is None:
        _text_embedder_manager = TextEmbedderManager()
    return _text_embedder_manager


def start_embedder(chat_types: list[str] | None = None) -> None:
    if chat_types is not None:
        _status.chat_types = list(chat_types)
    get_text_embedder_manager().start()


def stop_embedder() -> None:
    get_text_embedder_manager().stop()
