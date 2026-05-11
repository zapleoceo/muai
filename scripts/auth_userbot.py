"""One-time interactive Telethon auth. Run:
    docker compose run -it --rm bot python scripts/auth_userbot.py
"""
import asyncio
import os
import sys

sys.path.insert(0, "/app")

from telethon import TelegramClient

API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
PHONE = os.getenv("TELEGRAM_PHONE", "")

if not API_ID or not API_HASH or not PHONE:
    print("ERROR: set TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE in .env")
    sys.exit(1)

os.makedirs("/app/sessions", exist_ok=True)


async def main() -> None:
    client = TelegramClient("/app/sessions/userbot", API_ID, API_HASH)
    await client.start(phone=PHONE)
    me = await client.get_me()
    print(f"\n✅ Logged in as: {me.first_name} (@{me.username}, id={me.id})")
    print("Session saved to /app/sessions/userbot.session")
    await client.disconnect()


asyncio.run(main())
