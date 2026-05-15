import asyncio
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from typing import Any

from sqlalchemy import text

from app.db.database import AsyncSessionLocal
from app.llm.embedding import embed_gemini_multimodal, embed_text, inline_data_part, transcribe_audio_gemini
from app.services.chat_sync_settings_service import ChatSyncSettingsService
from app.services.plan_executor import build_message_link
from app.userbot.client import get_client

logger = logging.getLogger(__name__)

_PAGE_SIZE = 50
_MAX_BYTES = 5_000_000
_EMBED_DIMS = 512


@dataclass
class MediaEmbedderStatus:
    running: bool = False
    enabled: bool = False
    current_item: str = ""
    items_done: int = 0
    chunks_added: int = 0
    embed_ok: int = 0
    embed_failed: int = 0
    embed_multimodal_ok: int = 0
    embed_multimodal_failed: int = 0
    embed_text_ok: int = 0
    insert_ok: int = 0
    insert_conflict: int = 0
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
        settings_svc = ChatSyncSettingsService()
        sync_settings = await settings_svc.get()
        allowed_chat_types = list(sync_settings.get("allowed_types") or [])
        bl_raw = list(sync_settings.get("blacklist") or [])
        bl_ids: list[int] = []
        bl_usernames: list[str] = []
        for x in bl_raw:
            if isinstance(x, int):
                bl_ids.append(int(x))
                continue
            if isinstance(x, str):
                s = x.strip()
                if not s:
                    continue
                if s.isdigit():
                    bl_ids.append(int(s))
                    continue
                if s.startswith("@"):
                    s = s[1:]
                bl_usernames.append(s)

        async with AsyncSessionLocal() as session:
            total = (await session.execute(text("SELECT COUNT(*) FROM media_chunks"))).scalar() or 0
            pending = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT COUNT(*)
                            FROM messages m
                            JOIN chats c ON c.id = m.chat_id
                            LEFT JOIN media_chunks mc
                              ON mc.chat_id = m.chat_id AND mc.source_tg_msg_id = m.telegram_msg_id
                            WHERE m.telegram_msg_id IS NOT NULL
                              AND m.media_type IS NOT NULL
                              AND m.media_type = ANY(CAST(:types AS text[]))
                              AND c.type = ANY(CAST(:allowed_chat_types AS text[]))
                              AND NOT (m.chat_id = ANY(CAST(:bl_ids AS bigint[])))
                              AND NOT (c.username = ANY(CAST(:bl_usernames AS text[])))
                              AND mc.id IS NULL
                            """
                        ),
                        {
                            "types": self.status.types or [],
                            "allowed_chat_types": allowed_chat_types,
                            "bl_ids": bl_ids,
                            "bl_usernames": bl_usernames,
                        },
                    )
                ).scalar()
                or 0
            )
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

            if not self.status.types:
                self.status.enabled = False
                self.status.running = False
                self.status.errors.append("Не выбраны типы файлов для обработки.")
                continue

            if self.status.pending <= 0:
                self.status.enabled = False
                self.status.running = False
                continue

            await asyncio.sleep(0.5)

    async def _run_once(self) -> None:
        self.status.running = True
        self.status.current_item = ""
        self.status.items_done = 0
        self.status.chunks_added = 0
        self.status.embed_ok = 0
        self.status.embed_failed = 0
        self.status.embed_multimodal_ok = 0
        self.status.embed_multimodal_failed = 0
        self.status.embed_text_ok = 0
        self.status.insert_ok = 0
        self.status.insert_conflict = 0
        self.status.errors = []

        settings_svc = ChatSyncSettingsService()
        sync_settings = await settings_svc.get()
        allowed_chat_types = list(sync_settings.get("allowed_types") or [])
        bl_raw = list(sync_settings.get("blacklist") or [])
        bl_ids: list[int] = []
        bl_usernames: list[str] = []
        for x in bl_raw:
            if isinstance(x, int):
                bl_ids.append(int(x))
                continue
            if isinstance(x, str):
                s = x.strip()
                if not s:
                    continue
                if s.isdigit():
                    bl_ids.append(int(s))
                    continue
                if s.startswith("@"):
                    s = s[1:]
                bl_usernames.append(s)

        insert_sql = text(
            "INSERT INTO media_chunks "
            "(chat_id, chat_title, chat_username, source_msg_id, source_tg_msg_id, media_type, date_utc, chunk_text, embedding, meta) "
            "VALUES (:chat_id, :chat_title, :chat_username, :source_msg_id, :source_tg_msg_id, :media_type, :date_utc, :chunk_text, CAST(:emb AS vector(512)), CAST(:meta AS jsonb)) "
            "ON CONFLICT (chat_id, source_tg_msg_id) DO NOTHING "
            "RETURNING 1"
        )

        client = get_client()

        while True:
            if not self.status.enabled:
                break
            if not self.status.types:
                self.status.errors.append("Не выбраны типы файлов для обработки.")
                break

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
                          AND m.telegram_msg_id IS NOT NULL
                          AND m.media_type IS NOT NULL
                          AND m.media_type = ANY(CAST(:types AS text[]))
                          AND c.type = ANY(CAST(:allowed_chat_types AS text[]))
                          AND NOT (m.chat_id = ANY(CAST(:bl_ids AS bigint[])))
                          AND NOT (c.username = ANY(CAST(:bl_usernames AS text[])))
                        ORDER BY m.id ASC
                        LIMIT :lim
                        """
                    ),
                    {"types": self.status.types, "allowed_chat_types": allowed_chat_types, "bl_ids": bl_ids, "bl_usernames": bl_usernames, "lim": _PAGE_SIZE},
                )).mappings().all()

            if not rows:
                break

            grouped: dict[int, list[dict]] = {}
            for r in rows:
                chat_id = int(r["chat_id"])
                grouped.setdefault(chat_id, []).append(dict(r))

            pending_db: list[dict] = []
            for chat_id, items in grouped.items():
                entity: Any | None = None
                try:
                    entity = await client.get_entity(chat_id)
                except Exception:
                    entity = None

                ids = [int(i["tg_msg_id"]) for i in items if i.get("tg_msg_id")]
                tg_msgs = []
                if ids and entity is not None:
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

                    _is_audio = media_type in ("voice", "audio")
                    tg = tg_by_id.get(tg_msg_id) if tg_msg_id is not None else None
                    if tg is not None and getattr(tg, "media", None) is not None:
                        try:
                            raw_data = await client.download_media(tg, file=bytes)
                            if isinstance(raw_data, (bytes, bytearray)) and 0 < len(raw_data) <= _MAX_BYTES:
                                mime = getattr(getattr(tg, "file", None), "mime_type", None) or "application/octet-stream"
                                digest = sha256(raw_data).hexdigest()
                                meta.update({"mime_type": mime, "bytes": len(raw_data), "sha256": digest})

                                # For voice/audio: transcribe first, use text as chunk_text
                                if _is_audio:
                                    try:
                                        transcription = await transcribe_audio_gemini(mime_type=mime, data=bytes(raw_data))
                                        if transcription:
                                            chunk_text = f"{header}\n{transcription}"
                                            if link:
                                                chunk_text = f"{chunk_text}\n{link}"
                                            meta["transcription"] = transcription
                                    except Exception as exc:
                                        meta["transcription_error"] = str(exc)[:200]
                                        logger.warning("transcription failed chat=%s msg=%s: %s", chat_title, tg_msg_id, exc)

                                parts = [{"text": f"title: {chat_title} | text: {caption or meta.get('transcription') or 'none'}"}]
                                parts.append(inline_data_part(mime_type=mime, data=bytes(raw_data)))
                                try:
                                    emb = await embed_gemini_multimodal(parts=parts)
                                    if isinstance(emb, list) and len(emb) == _EMBED_DIMS and all(isinstance(x, (int, float)) and math.isfinite(float(x)) for x in emb):
                                        self.status.embed_multimodal_ok += 1
                                        self.status.embed_ok += 1
                                except Exception as exc:
                                    self.status.embed_multimodal_failed += 1
                                    meta["embed_error"] = str(exc)[:200]
                        except Exception as exc:
                            meta["download_error"] = str(exc)[:200]

                    if emb is None or not isinstance(emb, list) or len(emb) != _EMBED_DIMS or not all(isinstance(x, (int, float)) and math.isfinite(float(x)) for x in emb):
                        try:
                            # use embed_media tokens for fallback — respects capability checkboxes
                            # falls back to any embed token if no embed_media token available
                            emb = await embed_text(chunk_text, task_type="RETRIEVAL_DOCUMENT", capability="embed_media")
                            if isinstance(emb, list) and len(emb) == _EMBED_DIMS and all(isinstance(x, (int, float)) and math.isfinite(float(x)) for x in emb):
                                self.status.embed_text_ok += 1
                                self.status.embed_ok += 1
                        except Exception as exc:
                            meta["embed_text_error"] = str(exc)[:200]
                            self.status.embed_failed += 1
                            self.status.errors.append(f"embed_fail chat={chat_title} msg={tg_msg_id or msg_id}: {str(exc)[:200]}")
                            if "rate-limited" in str(exc).lower():
                                break
                            self.status.items_done += 1
                            continue

                    if not isinstance(emb, list) or len(emb) != _EMBED_DIMS:
                        self.status.embed_failed += 1
                        self.status.errors.append(f"bad_embedding_dim chat={chat_title} msg={tg_msg_id or msg_id}: {len(emb) if isinstance(emb, list) else 'none'}")
                        self.status.items_done += 1
                        continue

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
                try:
                    async with AsyncSessionLocal() as session:
                        inserted = 0
                        conflicts = 0
                        for row in pending_db:
                            res = await session.execute(insert_sql, row)
                            ok = res.scalar_one_or_none()
                            if ok:
                                inserted += 1
                            else:
                                conflicts += 1
                        await session.commit()
                    self.status.chunks_added += inserted
                    self.status.insert_ok += inserted
                    self.status.insert_conflict += conflicts
                except Exception as exc:
                    logger.exception("MediaEmbedder: failed to insert chunks")
                    self.status.errors.append(f"db_insert_error: {str(exc)[:200]}")
                    if "rate-limited" in str(exc).lower():
                        break
                    await asyncio.sleep(2.0)
                    continue

        self.status.running = False
        self.status.current_item = ""


_manager: MediaEmbedderManager | None = None


def get_media_embedder_manager() -> MediaEmbedderManager:
    global _manager
    if _manager is None:
        _manager = MediaEmbedderManager()
    return _manager
