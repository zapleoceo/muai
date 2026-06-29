"""Конвертирует Telethon SQLite session → StringSession и сохраняет в БД.

После этого ingestor-telegram читает строку из БД и не зависит от
файловой системы → нет sqlite3 baga в docker compose.

Запуск (один раз):
    docker run --rm --network host -v vera3_tg_sessions:/sessions ...
"""
import asyncio
import os

from sqlalchemy import select
from telethon import TelegramClient
from telethon.sessions import SQLiteSession, StringSession

from vera_shared.db.engine import get_session, init_engine
from vera_shared.db.models_sources import TelegramSessionRow
from vera_shared.crypto import encrypt


async def main():
    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    phone = os.environ["TELEGRAM_PHONE"]
    sqlite_path = os.environ.get("SQLITE_PATH", "/sessions/userbot.session")

    # Загружаем SQLite session
    print(f"Loading SQLite session: {sqlite_path}")
    sqlite_sess = SQLiteSession(sqlite_path)

    # Создаём client с SQLite session, забираем StringSession
    client = TelegramClient(sqlite_sess, api_id, api_hash)
    await client.connect()

    if not await client.is_user_authorized():
        print("✗ session not authorized")
        return

    me = await client.get_me()
    print(f"✓ Connected as {me.first_name} (@{me.username}, id={me.id})")

    # Конвертируем в StringSession
    string_sess = StringSession.save(client.session)
    print(f"StringSession length: {len(string_sess)}")
    await client.disconnect()

    # Сохраняем в БД (encrypted)
    await init_engine()
    session_enc = encrypt(string_sess)

    async with get_session() as s:
        existing = (await s.execute(
            select(TelegramSessionRow).where(TelegramSessionRow.phone == phone)
        )).scalar_one_or_none()
        if existing:
            existing.session_string_enc = session_enc
            existing.is_active = True
            print(f"✓ Updated existing session id={existing.id} for phone={phone}")
        else:
            s.add(TelegramSessionRow(
                phone=phone,
                session_string_enc=session_enc,
                is_active=True,
            ))
            print(f"✓ Created new session for phone={phone}")

    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
