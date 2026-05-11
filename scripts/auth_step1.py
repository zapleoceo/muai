"""Step 1: request auth code. Saves phone_code_hash for step 2."""
import asyncio, os, sys
sys.path.insert(0, "/app")
from telethon import TelegramClient

API_ID   = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
PHONE    = os.getenv("TELEGRAM_PHONE", "")

os.makedirs("/app/sessions", exist_ok=True)

async def main():
    client = TelegramClient("/app/sessions/userbot", API_ID, API_HASH)
    await client.connect()
    result = await client.send_code_request(PHONE)
    with open("/app/sessions/.phone_code_hash", "w") as f:
        f.write(result.phone_code_hash)
    print(f"OK: code sent to {PHONE}")
    await client.disconnect()

asyncio.run(main())
