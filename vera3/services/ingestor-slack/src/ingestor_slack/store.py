"""Состояние обхода Slack: каналы, треды, авторы. Весь SQL источника здесь.

Вставка событий и «автор → сущность» берутся из `vera_shared.ingest` — они
одинаковы у всех источников.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from vera_shared.db.engine import get_session
from vera_shared.db.models_sources import SlackConversationRow, SlackThreadRow
from vera_shared.ingest import insert_events, sync_author_entities

log = logging.getLogger("slack")


def kind_of(conversation: dict[str, Any]) -> str:
    if conversation.get("is_im"):
        return "im"
    if conversation.get("is_mpim"):
        return "mpim"
    return "channel"


async def upsert_conversations(
    conversations: list[dict[str, Any]], names: dict[str, str],
) -> list[SlackConversationRow]:
    """Синхронизировать список. Новый канал появляется сам, покинутый гаснет.

    Строка с курсором остаётся: возвращение в канал не должно начинать историю
    заново. Личка своего имени не имеет — берём имя собеседника.
    """
    seen: dict[str, dict[str, Any]] = {}
    for conv in conversations:
        cid = str(conv.get("id") or "")
        if not cid:
            continue
        kind = kind_of(conv)
        title = str(conv.get("name") or "")
        if kind == "im":
            peer = str(conv.get("user") or "")
            title = names.get(peer) or peer or "ЛС"
        seen[cid] = {"name": title[:255], "kind": kind,
                     "is_private": bool(conv.get("is_private") or kind in ("im", "mpim"))}

    async with get_session() as s:
        rows = list((await s.execute(select(SlackConversationRow))).scalars().all())
        known = {r.conversation_id: r for r in rows}
        for cid, fields in seen.items():
            row = known.get(cid)
            if row is None:
                row = SlackConversationRow(conversation_id=cid, is_active=True, **fields)
                s.add(row)
                rows.append(row)
                log.info("slack: новый канал «%s» (%s)", fields["name"], fields["kind"])
            else:
                row.name, row.kind = fields["name"], fields["kind"]
                row.is_private, row.is_active = fields["is_private"], True
        for row in rows:
            if row.conversation_id not in seen:
                row.is_active = False
    return [r for r in rows if r.is_active]


async def save_cursor(conversation_id: str, cursor: str | None, error: str | None) -> None:
    values: dict[str, Any] = {"last_polled_at": datetime.utcnow(), "last_error": error}
    if cursor:
        values["last_ts"] = cursor
    async with get_session() as s:
        await s.execute(
            update(SlackConversationRow)
            .where(SlackConversationRow.conversation_id == conversation_id)
            .values(**values)
        )


async def watch_thread(conversation_id: str, thread_ts: str,
                       latest_reply: str | None, activity: datetime) -> None:
    """Взять тред под наблюдение либо отметить в нём новую активность."""
    async with get_session() as s:
        row = (await s.execute(
            select(SlackThreadRow).where(
                SlackThreadRow.conversation_id == conversation_id,
                SlackThreadRow.thread_ts == thread_ts,
            )
        )).scalar_one_or_none()
        if row is None:
            s.add(SlackThreadRow(conversation_id=conversation_id, thread_ts=thread_ts,
                                 last_activity_at=activity))
        elif latest_reply and (row.last_reply_ts or "") < latest_reply:
            row.last_activity_at = activity


async def due_threads(conversation_id: str, *, limit: int,
                      watch_days: int) -> list[SlackThreadRow]:
    """Треды к проверке: сначала те, что дольше всех не проверялись.

    Ограничение по числу за прогон — сознательное: следить за КАЖДЫМ тредом
    каждые пять минут значило бы сотни лишних вызовов. Ответ в двухнедельном
    треде приходит с задержкой, а не теряется.
    """
    since = datetime.utcnow() - timedelta(days=watch_days)
    async with get_session() as s:
        return list((await s.execute(
            select(SlackThreadRow)
            .where(SlackThreadRow.conversation_id == conversation_id)
            .where(SlackThreadRow.last_activity_at >= since)
            .order_by(SlackThreadRow.last_polled_at.asc().nullsfirst())
            .limit(limit)
        )).scalars().all())


async def save_thread_cursor(thread_id: int, last_reply_ts: str | None,
                             activity: datetime | None) -> None:
    values: dict[str, Any] = {"last_polled_at": datetime.utcnow()}
    if last_reply_ts:
        values["last_reply_ts"] = last_reply_ts
    if activity is not None:
        values["last_activity_at"] = activity
    async with get_session() as s:
        await s.execute(
            update(SlackThreadRow).where(SlackThreadRow.id == thread_id).values(**values)
        )


def _author_of(spec: dict[str, Any], profiles: Any = None) -> dict[str, Any] | None:
    """Автор события → сущность. С профилем — ещё и связка с другими каналами.

    Рабочий email из профиля Slack — это же алиас gmail, поэтому человек
    прицепляется к УЖЕ существующей сущности вместо новой. До этого 25
    slack-сущностей были не связаны ни с чем, а Yevhenii Pavlenko лежал в графе
    тремя записями: по одной на канал.
    """
    meta = spec.get("metadata_") or {}
    if meta.get("author_role") == "self":
        return None
    sender = meta.get("sender_id")
    if not sender:
        return None

    entity: dict[str, Any] = {
        "identifier": f"user:{sender}",
        "name": str(meta.get("author_label") or sender),
    }
    profile = profiles.profile(str(sender)) if profiles is not None else {}
    if not profile:
        return entity

    attributes = {k: v for k, v in profile.items() if k != "is_bot"}
    attributes["slack_user_id"] = str(sender)
    entity["attributes"] = attributes
    email = (profile.get("email") or "").strip().lower()
    if email:
        entity["known_as"] = [("gmail", email)]
    return entity


async def save_events(specs: list[dict[str, Any]],
                      *, profiles: Any = None) -> list[dict[str, Any]]:
    """События + person-сущности их авторов. → реально вставленные события.

    Слияние УЖЕ существующих двойников остаётся за /entities/duplicates: это
    разрушительная операция, решать её должен владелец, а не ингестор.
    """
    fresh = await insert_events(specs)
    await sync_author_entities(
        fresh, source="slack",
        author_of=lambda spec: _author_of(spec, profiles))
    return fresh
