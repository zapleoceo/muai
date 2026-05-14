import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256

from sqlalchemy import text

from app.db.database import AsyncSessionLocal
from app.db.repository import MessageRepo
from app.llm.embedding import embed_gemini_multimodal, embed_text, inline_data_part
from app.services.chat_sync_settings_service import ChatSyncSettingsService
from app.services.plan_executor import build_message_link
from app.userbot.client import get_client

logger = logging.getLogger(__name__)

_PAGE_SIZE = 50
_MAX_BYTES = 5_000_000


@dataclass
class MediaEmbedderStatus:
    running: bool = False
    enabled: bool = False
    current_item: str = ""
    items_done: int = 0
    chunks_added: int = 0
    total_chunks: int = 0
    pending: int = 0
    last_run: datetime | None = None
    types: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class MediaEmbedderManager:
    def __init__(self) -> None:
        self.status = MediaEmbedderStatus()
        self._daemon_task: asyncio.Task | None = None
        self._run_task: asyncio.Task | None = None
        self._wake = asyncio.Event()

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

    def start(self, *, types: list[str]) -> None:
        self.status.types = [str(x) for x in (types or []) if str(x)]
        self.status.enabled = True
        self._wake.set()

    def stop(self) -> None:
        self.status.enabled = False
        task = self._run_task
        if task and not task.done():
            task.cancel()

    async def clear_chunks(self) -> int:
        self.stop()
        async with AsyncSessionLocal() as session:
            res = await session.execute(text("DELETE FROM media_chunks"))
            await session.commit()
        self.status.total_chunks = 0
        self.status.pending = 0
        return int(res.rowcount or 0)

    async def get_stats(self) -> dict:
        async with AsyncSessionLocal() as session:
            total = (await session.execute(text("SELECT COUNT(*) FROM media_chunks"))).scalar() or 0
            pending = (await session.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM messages m
                    JOIN chats c ON c.id = m.chat_id
                    LEFT JOIN media_chunks mc
                      ON mc.chat_id = m.chat_id AND mc.source_tg_msg_id = m.telegram_msg_id
                    WHERE m.media_type IS NOT NULL
                      AND mc.id IS NULL
                    """
                )
            )).scalar() or 0
        self.status.total_chunks = int(total)
        self.status.pending = int(pending)
        return {"total_chunks": int(total), "pending": int(pending)}

    async def _run_daemon(self) -> None:
        await self.get_stats()
        while True:
            if not self.status.enabled:
                self.status.running = False
                self._wake.clear()
                await self._wake.wait()
                continue

            if self._run_task and not self._run_task.done():
                self._wake.clear()
                await self._wake.wait()
                continue

            self._wake.clear()
            self._run_task = asyncio.create_task(self._run_once())
            try:
                await self._run_task
            except asyncio.CancelledError:
                self.status.running = False
            except Exception:
                logger.exception("MediaEmbedder loop error")
                self.status.running = False

            self.status.last_run = datetime.now()
            await self.get_stats()

            if not self.status.enabled:
                continue
            self.status.enabled = False

    async def _run_once(self) -> None:
        self.status.running = True
        self.status.current_item = ""
        self.status.items_done = 0
        self.status.chunks_added = 0
        self.status.errors = []

        settings_svc = ChatSyncSettingsService()
        sync_settings = await settings_svc.get()

        insert_sql = text(
            "INSERT INTO media_chunks "
            "(chat_id, chat_title, chat_username, source_msg_id, source_tg_msg_id, media_type, date_utc, chunk_text, embedding, meta) "
            "VALUES (:chat_id, :chat_title, :chat_username, :source_msg_id, :source_tg_msg_id, :media_type, :date_utc, :chunk_text, CAST(:emb AS vector), CAST(:meta AS jsonb)) "
            "ON CONFLICT (chat_id, source_tg_msg_id) DO NOTHING"
        )

        client = get_client()

        while True:
            async with AsyncSessionLocal() as session:
                rows = (await session.execute(
                    text(
                        """
                        SELECT
                            m.id AS msg_id,
                            m.chat_id AS chat_id,
                            m.telegram_msg_id AS tg_msg_id,
                            m.date_utc AS date_utc,
                            m.media_type AS media_type,
                            m.text AS text,
                            m.caption AS caption,
                            m.direction AS direction,
                            c.type AS chat_type,
                            c.title AS chat_title,
                            c.username AS chat_username
                        FROM messages m
                        JOIN chats c ON c.id = m.chat_id
                        LEFT JOIN media_chunks mc
                          ON mc.chat_id = m.chat_id AND mc.source_tg_msg_id = m.telegram_msg_id
                        WHERE mc.id IS NULL
                          AND m.media_type IS NOT NULL
                          AND m.media_type = ANY(CAST(:types AS text[]))
                        ORDER BY m.id ASC
                        LIMIT :lim
                        """
                    ),
                    {"types": self.status.types, "lim": _PAGE_SIZE},
                )).mappings().all()

            if not rows:
                break

            grouped: dict[int, list[dict]] = {}
            for r in rows:
                chat_id = int(r["chat_id"])
                grouped.setdefault(chat_id, []).append(dict(r))

            pending_db: list[dict] = []
            for chat_id, items in grouped.items():
                if settings_svc.is_blacklisted(int(chat_id), items[0].get("chat_username"), sync_settings):
                    continue
                if not settings_svc.type_allowed(str(items[0].get("chat_type") or ""), sync_settings):
                    continue

                try:
                    entity = await client.get_entity(chat_id)
                except Exception:
                    continue

                ids = [int(i["tg_msg_id"]) for i in items if i.get("tg_msg_id")]
                tg_msgs = []
                if ids:
                    try:
                        tg_msgs = await client.get_messages(entity, ids=ids)
                    except Exception:
                        tg_msgs = []
                tg_by_id = {int(m.id): m for m in tg_msgs if m is not None and getattr(m, "id", None) is not None}

                for it in items:
                    msg_id = int(it["msg_id"])
                    tg_msg_id = int(it["tg_msg_id"]) if it.get("tg_msg_id") is not None else None
                    media_type = str(it.get("media_type") or "")
                    chat_title = str(it.get("chat_title") or chat_id)
                    chat_username = it.get("chat_username")
                    chat_type = str(it.get("chat_type") or "")
                    date_utc = it.get("date_utc")
                    caption = it.get("text") or it.get("caption") or ""
                    link = build_message_link(
                        chat_id=int(chat_id),
                        chat_type=chat_type,
                        chat_username=chat_username,
                        telegram_msg_id=tg_msg_id,
                    )
                    header = f"[Чат: {chat_title} | {chat_type} | {media_type}]"
                    chunk_text = f"{header}\n{caption}".strip()
                    if link:
                        chunk_text = f"{chunk_text}\n{link}"

                    self.status.current_item = f"{chat_title} / {media_type}"

                    emb: list[float] | None = None
                    meta: dict = {
                        "kind": "media",
                        "media_type": media_type,
                        "chat_type": chat_type,
                        "source_msg_id": msg_id,
                        "source_tg_msg_id": tg_msg_id,
                        "link": link,
                    }

                    tg = tg_by_id.get(tg_msg_id) if tg_msg_id is not None else None
                    if tg is not None and getattr(tg, "media", None) is not None:
                        try:
                            data = await client.download_media(tg, file=bytes)
                            if isinstance(data, (bytes, bytearray)) and 0 < len(data) <= _MAX_BYTES:
                                mime = getattr(getattr(tg, "file", None), "mime_type", None) or "application/octet-stream"
                                digest = sha256(data).hexdigest()
                                meta.update({"mime_type": mime, "bytes": len(data), "sha256": digest})
                                parts = [{"text": f"title: {chat_title} | text: {caption or 'none'}"}]
                                parts.append(inline_data_part(mime_type=mime, data=bytes(data)))
                                try:
                                    emb = await embed_gemini_multimodal(parts=parts)
                                except Exception as exc:
                                    meta["embed_error"] = str(exc)[:200]
                        except Exception as exc:
                            meta["download_error"] = str(exc)[:200]

                    if emb is None:
                        emb = await embed_text(chunk_text, task_type="RETRIEVAL_DOCUMENT")

                    vec_str = "[" + ",".join(str(x) for x in emb) + "]"
                    pending_db.append(
                        {
                            "chat_id": int(chat_id),
                            "chat_title": chat_title,
                            "chat_username": chat_username,
                            "source_msg_id": msg_id,
                            "source_tg_msg_id": tg_msg_id,
                            "media_type": media_type,
                            "date_utc": date_utc,
                            "chunk_text": chunk_text,
                            "emb": vec_str,
                            "meta": json.dumps(meta, ensure_ascii=False),
                        }
                    )
                    self.status.items_done += 1

            if pending_db:
                async with AsyncSessionLocal() as session:
                    await session.execute(insert_sql, pending_db)
                    await session.commit()
                self.status.chunks_added += len(pending_db)

        self.status.running = False
        self.status.current_item = ""


_manager: MediaEmbedderManager | None = None


def get_media_embedder_manager() -> MediaEmbedderManager:
    global _manager
    if _manager is None:
        _manager = MediaEmbedderManager()
    return _manager

