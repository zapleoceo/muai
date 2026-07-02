"""Синхронизация принадлежности к проектам из папок Telegram + правил имён.

Что делает (идемпотентно, можно гонять по cron):
  1. Читает папки Telegram (userbot) → чаты папки «ItStep» = проект itstep.
  2. Правило имён: чаты с «veranda»/«веранда» в названии = проект veranda.
  3. Пишет всё в project_membership (kind=chat) + account-правила (kind=account).
  4. Проставляет events.project по этой карте (chat + gmail account).
  5. Выводит людей: sender_id из чатов проекта → project_membership (kind=person).
     Человек может попасть в несколько проектов (в основном не пересекаются).

Запуск:
  docker exec vera3-ingestor-telegram python -m ingestor_telegram.sync_projects
"""
from __future__ import annotations

import asyncio
import logging

from ingestor_telegram.userbot import API_ID, API_HASH, load_session_string
from sqlalchemy import text
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.messages import GetDialogFiltersRequest
from vera_shared.db.engine import get_session, init_engine
from vera_shared.projects.rules import (
    ACCOUNT_RULES, NAME_RULES, OWNER_TG_ID, chat_id_canon_sql, chat_key,
    folder_to_project,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("sync-projects")


async def _upsert_chat(project: str, key: int, label: str | None, source: str) -> None:
    async with get_session() as s:
        await s.execute(text("""
            INSERT INTO project_membership (project, kind, key, label, source, updated_at)
            VALUES (:p, 'chat', :k, :l, :src, now())
            ON CONFLICT (project, kind, key)
            DO UPDATE SET label=EXCLUDED.label, source=EXCLUDED.source, updated_at=now()
        """), {"p": project, "k": str(key), "l": label, "src": source})


async def sync_folders(client: TelegramClient) -> int:
    """Папки Telegram → project_membership(kind=chat)."""
    res = await client(GetDialogFiltersRequest())
    filters = getattr(res, "filters", res)
    n = 0
    for f in filters:
        title = getattr(f, "title", None)
        if title is None:
            continue
        title_txt = getattr(title, "text", title)
        project = folder_to_project(str(title_txt))
        if not project:
            continue
        for peer in (getattr(f, "include_peers", []) or []):
            try:
                ent = await client.get_entity(peer)
            except Exception as e:
                log.warning("peer resolve failed: %s", e)
                continue
            label = getattr(ent, "title", None) or (
                (getattr(ent, "first_name", "") or "") + " "
                + (getattr(ent, "last_name", "") or "")).strip()
            await _upsert_chat(project, chat_key(ent.id), label,
                               f"folder:{title_txt}")
            n += 1
    log.info("Folders synced: %d chats", n)
    return n


async def sync_name_rules() -> int:
    """Чаты с проектным словом в названии → project_membership(kind=chat)."""
    total = 0
    async with get_session() as s:
        for project, subs in NAME_RULES.items():
            like = " OR ".join([f"LOWER(metadata->>'chat_title') LIKE :s{i}"
                                for i in range(len(subs))])
            params = {f"s{i}": f"%{sub}%" for i, sub in enumerate(subs)}
            rows = (await s.execute(text(f"""
                SELECT DISTINCT {chat_id_canon_sql()} AS cid,
                       MAX(metadata->>'chat_title') AS title
                FROM events
                WHERE source='telegram' AND metadata->>'chat_id' IS NOT NULL
                  AND ({like})
                GROUP BY 1
            """), params)).all()
            for cid, title in rows:
                await _upsert_chat(project, int(cid), title, f"name:{project}")
                total += 1
    log.info("Name-rule chats synced: %d", total)
    return total


async def sync_accounts() -> None:
    """Gmail-аккаунты → project_membership(kind=account)."""
    async with get_session() as s:
        for project, patterns in ACCOUNT_RULES.items():
            for pat in patterns:
                await s.execute(text("""
                    INSERT INTO project_membership (project, kind, key, label, source, updated_at)
                    VALUES (:p, 'account', :k, :l, 'rule:account', now())
                    ON CONFLICT (project, kind, key)
                    DO UPDATE SET updated_at=now()
                """), {"p": project, "k": pat, "l": f"{project} email"})


async def assign_events() -> None:
    """events.project ← project_membership (chat + account)."""
    async with get_session() as s:
        r1 = await s.execute(text(f"""
            UPDATE events e SET project = pm.project
            FROM project_membership pm
            WHERE pm.kind='chat' AND e.source='telegram'
              AND e.metadata->>'chat_id' IS NOT NULL
              AND {chat_id_canon_sql('e')} = pm.key::bigint
              AND (e.project IS DISTINCT FROM pm.project)
        """))
        r2 = await s.execute(text("""
            UPDATE events e SET project = pm.project
            FROM project_membership pm
            WHERE pm.kind='account' AND e.source='gmail'
              AND e.account ILIKE pm.key
              AND (e.project IS DISTINCT FROM pm.project)
        """))
        # Очистка ложных LLM-догадок: для telegram проекты itstep/veranda
        # определяются ТОЛЬКО членством (папка/имя). Если чат не в membership,
        # а LLM налепил itstep/veranda — это ошибка, сбрасываем в 'other'.
        r3 = await s.execute(text(f"""
            UPDATE events e SET project = 'other'
            WHERE e.source='telegram' AND e.project IN ('itstep','veranda')
              AND (e.metadata->>'chat_id') IS NOT NULL
              AND {chat_id_canon_sql('e')} NOT IN (
                  SELECT key::bigint FROM project_membership WHERE kind='chat')
        """))
    log.info("events.project reassigned: %s tg chats, %s gmail, %s false→other",
             r1.rowcount, r2.rowcount, r3.rowcount)


async def derive_people() -> None:
    """Люди из чатов проекта → project_membership(kind=person). Исключаем владельца.

    Полная пересборка: сначала удаляем прежних derived-людей (иначе после
    очистки ложных проектов остались бы устаревшие привязки), потом заново.
    """
    async with get_session() as s:
        await s.execute(text("DELETE FROM project_membership WHERE kind='person'"))
        r = await s.execute(text("""
            INSERT INTO project_membership (project, kind, key, label, source, updated_at)
            SELECT e.project, 'person',
                   e.metadata->>'sender_id',
                   MAX(e.metadata->>'sender_username'),
                   'derived:chat', now()
            FROM events e
            WHERE e.source='telegram' AND e.project IS NOT NULL
              AND e.project IN ('itstep','veranda')
              AND e.metadata->>'sender_id' IS NOT NULL
              AND e.metadata->>'sender_id' <> :owner
            GROUP BY e.project, e.metadata->>'sender_id'
            ON CONFLICT (project, kind, key)
            DO UPDATE SET label=EXCLUDED.label, updated_at=now()
        """), {"owner": str(OWNER_TG_ID)})
    log.info("People derived: %s rows", r.rowcount)


async def main() -> None:
    await init_engine()
    ss = await load_session_string()
    client = TelegramClient(StringSession(ss), API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        log.error("userbot not authorized")
        return
    try:
        await sync_folders(client)
    finally:
        await client.disconnect()
    await sync_name_rules()
    await sync_accounts()
    await assign_events()
    await derive_people()
    log.info("sync_projects DONE")


if __name__ == "__main__":
    asyncio.run(main())
