"""Claim-batch DB query + group-chat classification/chunking for batching."""
from __future__ import annotations

from sqlalchemy import text
from vera_shared.db.engine import get_session
from vera_shared.db.models import EventRow

from brain_triage.config import (
    BATCH_SIZE,
    TRIAGE_GROUP_BATCH_MAX_CHARS,
    TRIAGE_GROUP_BATCH_SIZE,
)


async def _claim_batch(limit: int = BATCH_SIZE) -> list[EventRow]:
    """Захватить batch событий ОДНИМ запросом через UPDATE ... RETURNING *.

    Старый код делал UPDATE + второй SELECT — между ними окно для гонки/потери
    видимости. Теперь всё в одной сессии, и `triage_started_at = NOW()` ставится
    атомарно вместе с claim'ом. `limit` урезается rate-лимитером бэкфилла.
    """
    if limit <= 0:
        return []
    async with get_session() as s:
        rs = await s.execute(text(
            """
            UPDATE events
            SET triage_status = 'processing',
                triage_started_at = NOW()
            WHERE id IN (
              SELECT id FROM events
              WHERE triage_status = 'pending' AND content_text != ''
              ORDER BY occurred_at DESC
              LIMIT :batch
              FOR UPDATE SKIP LOCKED
            )
            RETURNING id, source, source_event_id, account, category,
                      content_text, occurred_at, importance, metadata,
                      triage_started_at, triage_error
            """
        ), {"batch": limit})
        mappings = list(rs.mappings().all())

    if not mappings:
        return []

    # Лёгкие detached объекты — не нужно второй SELECT, ORM не требуется
    out: list[EventRow] = []
    for m in mappings:
        ev = EventRow(
            id=m["id"], source=m["source"], source_event_id=m["source_event_id"],
            account=m["account"], category=m["category"],
            content_text=m["content_text"], occurred_at=m["occurred_at"],
            importance=m["importance"], metadata_=m["metadata"],
            # для fence финальных UPDATE'ов и исключения batch-miss из групп
            triage_started_at=m["triage_started_at"],
            triage_error=m["triage_error"],
        )
        out.append(ev)
    return out


def chat_kind(row: EventRow) -> str:
    """private | group | channel | other — из metadata события.

    Новые события (после фикса is_channel/is_group) несут явное
    metadata.chat_kind. Для существующего backlog (записан до фикса)
    считаем то же самое из старых chat_type/is_supergroup — ничего не
    теряем, весь текущий backlog классифицируем на лету, без миграции.
    """
    meta = row.metadata_ or {}
    if "chat_kind" in meta:
        return meta["chat_kind"] or "other"
    chat_type = meta.get("chat_type")
    if chat_type == "user":
        return "private"
    if chat_type in {"chat", "chatfull"}:
        return "group"
    if chat_type == "channel":
        return "group" if meta.get("is_supergroup") else "channel"
    return "other"


def _chunk_group_rows(
    rows: list[EventRow],
    max_batch: int = TRIAGE_GROUP_BATCH_SIZE,
    max_chars: int = TRIAGE_GROUP_BATCH_MAX_CHARS,
) -> list[list[EventRow]]:
    """Пачки для группового батчинга: до max_batch событий, но не больше
    max_chars суммарного текста — один аномально длинный текст не должен
    раздувать один LLM-вызов."""
    chunks: list[list[EventRow]] = []
    current: list[EventRow] = []
    current_chars = 0
    for row in rows:
        length = len(row.content_text or "")
        if current and (len(current) >= max_batch or current_chars + length > max_chars):
            chunks.append(current)
            current, current_chars = [], 0
        current.append(row)
        current_chars += length
    if current:
        chunks.append(current)
    return chunks
