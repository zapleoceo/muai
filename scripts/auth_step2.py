"""Step 2: complete auth with the received code.
Usage: python scripts/auth_step2.py 12345
       python scripts/auth_step2.py 12345 my2fapassword
"""
import asyncio, os, sys
sys.path.insert(0, "/app")
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

API_ID   = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
PHONE    = os.getenv("TELEGRAM_PHONE", "")
CODE     = sys.argv[1] if len(sys.argv) > 1 else ""
PASSWORD = sys.argv[2] if len(sys.argv) > 2 else ""

async def main():
    with open("/app/sessions/.phone_code_hash") as f:
        phone_code_hash = f.read().strip()

    client = TelegramClient("/app/sessions/userbot", API_ID, API_HASH)
    await client.connect()
    try:
        await client.sign_in(PHONE, CODE, phone_code_hash=phone_code_hash)
    except SessionPasswordNeededError:
        if not PASSWORD:
            print("2FA enabled — run again with password as second argument:")
            print(f"  python scripts/auth_step2.py {CODE} YOUR_2FA_PASSWORD")
            await client.disconnect()
            return
        await client.sign_in(password=PASSWORD)

    me = await client.get_me()
    print(f"OK: logged in as {me.first_name} (@{me.username}, id={me.id})")
    await client.disconnect()

asyncio.run(main())
