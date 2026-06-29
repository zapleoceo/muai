"""Логин в Instagram через instagrapi + сохранение session JSON в БД.

Использование:
    stage=login: ждёт IG_USERNAME / IG_PASSWORD из env, может потребовать
                 challenge_code через /tmp/ig_challenge.

После успеха в БД появится InstagramSessionRow с session_json_enc (cookies+device).
"""
import asyncio
import json
import os
import sys
import time

from sqlalchemy import select
from instagrapi import Client
from instagrapi.exceptions import (
    BadPassword, ChallengeRequired, TwoFactorRequired,
)

from vera_shared.db.engine import get_session, init_engine
from vera_shared.db.models_sources import InstagramSessionRow
from vera_shared.crypto import encrypt


CHALLENGE_FILE = "/tmp/ig_challenge"   # код из почты/SMS
TFA_FILE = "/tmp/ig_2fa"               # 6-значный TOTP
STATE_FILE = "/tmp/ig_state.json"      # промежуточный device fingerprint


def _wait_for(path: str, timeout: int = 600) -> str:
    """Polling-style ожидание файла (пользователь пишет код извне)."""
    print(f"Waiting for {path} (timeout {timeout}s)…", flush=True)
    start = time.time()
    while time.time() - start < timeout:
        if os.path.exists(path):
            with open(path) as f:
                v = f.read().strip()
            os.remove(path)
            return v
        time.sleep(2)
    raise TimeoutError(f"{path} not provided in {timeout}s")


def challenge_code_handler(username: str, choice) -> str:
    print(f"⚠ challenge required for {username}, choice={choice}", flush=True)
    print(f"Write 6-digit code to {CHALLENGE_FILE}", flush=True)
    return _wait_for(CHALLENGE_FILE)


def two_factor_code_handler() -> str:
    print(f"⚠ 2FA TOTP required. Write 6 digits to {TFA_FILE}", flush=True)
    return _wait_for(TFA_FILE)


async def main():
    username = os.environ["IG_USERNAME"]
    password = os.environ["IG_PASSWORD"]
    proxy = os.environ.get("IG_PROXY")

    cl = Client()
    cl.delay_range = [2, 5]  # антибан
    cl.challenge_code_handler = challenge_code_handler
    cl.two_factor_code_handler = two_factor_code_handler
    if proxy:
        cl.set_proxy(proxy)

    # Восстановить device если есть прежний state
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            settings = json.load(f)
        cl.set_settings(settings)
        print("Restored prior device fingerprint", flush=True)

    print(f"Logging in as @{username}…", flush=True)
    try:
        cl.login(username, password)
    except BadPassword as e:
        print(f"✗ bad password: {e}")
        return
    except TwoFactorRequired:
        # instagrapi умеет сам через handler, но если нет — fallback
        code = two_factor_code_handler()
        cl.login(username, password, verification_code=code)
    except ChallengeRequired as e:
        print(f"✗ challenge wasn't resolved: {e}")
        return
    except Exception as e:
        print(f"✗ unexpected: {type(e).__name__}: {e}")
        return

    user_id = cl.user_id
    me = cl.user_info(user_id)
    print(f"✓ Logged in as @{me.username} (id={user_id}, full={me.full_name})", flush=True)

    settings = cl.get_settings()
    # сохраним локально для следующего логина (device fingerprint stability)
    with open(STATE_FILE, "w") as f:
        json.dump(settings, f)

    session_json = json.dumps(settings)
    enc = encrypt(session_json)

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
