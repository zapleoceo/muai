"""Логин в Instagram через готовый sessionid из браузера → сохранить в БД."""
import asyncio
import json
import os

from sqlalchemy import select
from instagrapi import Client

from vera_shared.db.engine import get_session, init_engine
from vera_shared.db.models_sources import InstagramSessionRow
from vera_shared.crypto import encrypt


async def main():
    sessionid = os.environ["IG_SESSIONID"]

    cl = Client()
    cl.delay_range = [2, 5]

    print("Restoring session from sessionid…", flush=True)
    cl.login_by_sessionid(sessionid)

    user_id = cl.user_id
    me = cl.user_info(user_id)
    print(f"✓ Logged in as @{me.username} (id={user_id}, full={me.full_name})", flush=True)

    # Пробуем сразу запросить direct inbox — проверка что API реально работает
    print("Fetching DM inbox preview…", flush=True)
    inbox = cl.direct_threads(amount=3)
    print(f"✓ inbox: {len(inbox)} threads", flush=True)
    for t in inbox[:3]:
        last = t.messages[0] if t.messages else None
        print(f"  thread {t.id}: users={[u.username for u in t.users]}, "
              f"last={last.text[:60] if last and last.text else '(media)'}", flush=True)

    settings = cl.get_settings()
    enc = encrypt(json.dumps(settings))

    await init_engine()
    async with get_session() as s:
        existing = (await s.execute(
            select(InstagramSessionRow).where(InstagramSessionRow.username == me.username)
        )).scalar_one_or_none()
        if existing:
            existing.session_json_enc = enc
            existing.is_active = True
            print(f"✓ Updated row id={existing.id}", flush=True)
        else:
            s.add(InstagramSessionRow(username=me.username, session_json_enc=enc,
                                       is_active=True))
            print(f"✓ Inserted new row for @{me.username}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
