"""Создать новую Telethon StringSession через SMS-логин.

Шаг 1: запрашиваем код, ждём от пользователя.
Шаг 2: после ввода кода (через файл /tmp/tg_code) логинимся и пишем в БД
        под отдельным label='userbot-vera3', чтобы не конфликтовать с MCP.
"""
import asyncio
import os
import sys

from sqlalchemy import select
from telethon import TelegramClient
from telethon.sessions import StringSession

from vera_shared.db.engine import get_session, init_engine
from vera_shared.db.models_sources import TelegramSessionRow
from vera_shared.crypto import encrypt


API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
PHONE = os.environ["TELEGRAM_PHONE"]
CODE_FILE = "/tmp/tg_code"
PWD_FILE = "/tmp/tg_pwd"


async def main():
    stage = sys.argv[1] if len(sys.argv) > 1 else "request"
    state_file = "/tmp/tg_state"

    client = TelegramClient(StringSession(), API_ID, API_HASH,
                             device_model="Vera 3.0 ingestor",
                             system_version="docker",
                             app_version="3.0")
    await client.connect()

    if stage == "request":
        print(f"Sending code to {PHONE}…")
        sent = await client.send_code_request(PHONE)
        with open(state_file, "w") as f:
            f.write(StringSession.save(client.session) + "\n" + sent.phone_code_hash)
        print(f"✓ Code sent. phone_code_hash={sent.phone_code_hash}")
        print(f"Now: write 5-digit SMS code to {CODE_FILE} then run with arg=login")
        return

    # stage == 'login'
    if not os.path.exists(state_file):
        print(f"✗ no state file — run with arg=request first")
        return
    if not os.path.exists(CODE_FILE):
        print(f"✗ code file missing: {CODE_FILE}")
        return

    with open(state_file) as f:
        lines = f.read().strip().split("\n")
    session_str, phone_code_hash = lines[0], lines[1]
    with open(CODE_FILE) as f:
        code = f.read().strip()

    # Restore client with stored session
    await client.disconnect()
    client = TelegramClient(StringSession(session_str), API_ID, API_HASH,
                             device_model="Vera 3.0 ingestor",
                             system_version="docker",
                             app_version="3.0")
    await client.connect()

    try:
        await client.sign_in(PHONE, code, phone_code_hash=phone_code_hash)
    except Exception as e:
        if "password" in str(e).lower() or "2FA" in str(e) or "two-step" in str(e).lower():
            if not os.path.exists(PWD_FILE):
                print(f"✗ 2FA password required — write to {PWD_FILE}")
                return
            with open(PWD_FILE) as f:
                pwd = f.read().strip()
            await client.sign_in(password=pwd)
        else:
            print(f"✗ sign_in failed: {e}")
            return

    me = await client.get_me()
    print(f"✓ Logged in as {me.first_name} (@{me.username}, id={me.id})")
    string_sess = StringSession.save(client.session)
    print(f"StringSession length: {len(string_sess)}")
    await client.disconnect()

    # Save to DB — deactivate previous, insert new for ingestor
    await init_engine()
    enc = encrypt(string_sess)
    async with get_session() as s:
        existing = (await s.execute(
            select(TelegramSessionRow).where(TelegramSessionRow.phone == PHONE)
        )).scalar_one_or_none()
        if existing:
            existing.session_string_enc = enc
            existing.is_active = True
            print(f"✓ Updated existing row id={existing.id} for {PHONE}")
        else:
            s.add(TelegramSessionRow(phone=PHONE, session_string_enc=enc, is_active=True))
            print(f"✓ Inserted new session for {PHONE}")

    # cleanup
    for f in (CODE_FILE, PWD_FILE, state_file):
        if os.path.exists(f):
            os.remove(f)


if __name__ == "__main__":
    asyncio.run(main())
