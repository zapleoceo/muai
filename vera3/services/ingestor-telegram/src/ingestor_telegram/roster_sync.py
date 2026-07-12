"""Roster sync — молчуны проектных групп в граф идентичности.

В граф обычно попадают только те, кто хоть раз ПИСАЛ (entity_sync идёт по
сообщениям). Участники-лурκеры невидимы. Этот модуль по явной команде
владельца (кнопка на /entities/duplicates) опрашивает get_participants
ТОЛЬКО для чатов из project_membership (рабочие группы, ~10-15 штук) и
создаёт для молчунов Entity + Membership.

Anti-ban: один чат за раз, пауза ROSTER_CHAT_DELAY_S между чатами, потолок
ROSTER_MEMBER_CAP участников на чат, жёсткий backoff на FloodWait. Никаких
кронов — только явный запуск.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from sqlalchemy import text
from telethon.errors import ChatAdminRequiredError, FloodWaitError
from telethon.tl.types import PeerChannel, PeerChat
from vera_shared.db.engine import get_session
from vera_shared.graph.repo import upsert_entity, upsert_membership

from ingestor_telegram.entity_sync import _person_name

log = logging.getLogger("tg.roster")

CHAT_DELAY_S = float(os.environ.get("ROSTER_CHAT_DELAY_S", "20"))
MEMBER_CAP = int(os.environ.get("ROSTER_MEMBER_CAP", "500"))

# Один прогон за раз; состояние живёт в процессе юзербота.
state: dict[str, Any] = {"running": False, "last": None}


async def project_chats() -> list[dict[str, Any]]:
    """Групповые чаты проектов: entity_id + tg_id + тип. Источник —
    project_membership (kind='chat'), сматченный на граф через alias."""
    async with get_session() as s:
        rows = (await s.execute(text("""
            SELECT DISTINCT e.id AS entity_id, e.name, e.type,
                   (e.attributes->>'tg_id') AS tg_id
            FROM project_membership pm
            JOIN entity_aliases a
              ON a.source = 'telegram' AND a.identifier = 'chat:' || pm.key
            JOIN entities e ON e.id = a.entity_id
            WHERE pm.kind = 'chat'
              AND e.type IN ('group', 'supergroup')
        """))).mappings().all()
    return [dict(r) for r in rows]


def _peer_of(chat: dict[str, Any]):
    tg_id = int(chat["tg_id"])
    return PeerChannel(tg_id) if chat["type"] == "supergroup" else PeerChat(tg_id)


async def sync_chat_roster(client: Any, chat: dict[str, Any],
                           member_cap: int = MEMBER_CAP) -> int:
    """Участники одного чата → Entity + Membership. Возвращает число людей."""
    participants = await client.get_participants(_peer_of(chat), limit=member_cap)
    synced = 0
    for u in participants:
        if getattr(u, "bot", False) or getattr(u, "deleted", False):
            continue
        person_id = await upsert_entity(
            type="person", name=_person_name(u),
            source="telegram", identifier=f"user:{u.id}",
            attributes={"tg_id": u.id,
                        "username": getattr(u, "username", None),
                        "is_bot": False},
        )
        await upsert_membership(
            parent_entity_id=chat["entity_id"], child_entity_id=person_id,
            source="telegram", role="member",
            attributes={"observed_via": "roster_sync"},
        )
        synced += 1
    return synced


async def run_roster_sync(client: Any) -> dict[str, Any]:
    """Полный прогон по проектным чатам. Сбой одного чата не роняет прогон."""
    if state["running"]:
        return {"status": "already_running"}
    state["running"] = True
    stats: dict[str, Any] = {"chats": 0, "people": 0, "errors": 0, "skipped": []}
    try:
        chats = await project_chats()
        log.info("roster sync: %d project chats", len(chats))
        for chat in chats:
            try:
                n = await sync_chat_roster(client, chat)
                stats["chats"] += 1
                stats["people"] += n
                log.info("roster: %s → %d участников", chat["name"], n)
            except FloodWaitError as e:
                log.warning("roster FloodWait %ss на «%s» — backing off",
                            e.seconds, chat["name"])
                await asyncio.sleep(e.seconds + 5)
                stats["errors"] += 1
            except ChatAdminRequiredError:
                stats["skipped"].append(chat["name"])
                log.info("roster: «%s» скрывает участников (нужны админ-права)",
                         chat["name"])
            except Exception as e:
                stats["errors"] += 1
                log.warning("roster: «%s» failed: %s", chat["name"], e)
            await asyncio.sleep(CHAT_DELAY_S)
    finally:
        state["running"] = False
        state["last"] = stats
    log.info("roster sync done: %s", stats)
    return stats
