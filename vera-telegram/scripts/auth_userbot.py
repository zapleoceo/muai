"""One-time interactive Telethon session authorizer.

Run from vera-telegram container:
  docker exec -it vera-vera-telegram-1 python3 scripts/auth_userbot.py

Or on the server directly:
  docker run --rm -it -v /var/www/vera/data:/data \
    --env-file /var/www/vera/.env \
    vera-vera-telegram-1 python3 scripts/auth_userbot.py
"""
import asyncio
import os
import sys

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError


async def main() -> None:
    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    phone = os.environ["TELEGRAM_PHONE"]
    session_path = os.environ.get("SESSION_PATH", "/data/sessions/userbot")

    # Strip .session suffix if present — Telethon appends it automatically
    if session_path.endswith(".session"):
        session_path = session_path[: -len(".session")]

    os.makedirs(os.path.dirname(session_path), exist_ok=True)
    client = TelegramClient(session_path, api_id, api_hash)

    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Already authorized as {me.first_name} (id={me.id})")
        await client.disconnect()
        return

    print(f"Sending code to {phone}...")
    await client.send_code_request(phone)
    code = input("Enter the code you received: ").strip()

    try:
        await client.sign_in(phone, code)
    except SessionPasswordNeededError:
        password = input("2FA password required: ").strip()
        await client.sign_in(password=password)

    me = await client.get_me()
    print(f"Authorized as {me.first_name} (id={me.id})")
    await client.disconnect()
    print(f"Session saved to {session_path}.session")


if __name__ == "__main__":
    asyncio.run(main())
