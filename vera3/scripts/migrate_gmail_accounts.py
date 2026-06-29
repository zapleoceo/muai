"""Миграция Gmail OAuth accounts из Vera 2.0 SQLite в Vera 3.0 Postgres.

Расшифровывает refresh_token_enc Vera 2.0 SESSION_SECRET'ом и перешифровывает
Vera 3.0 TOKEN_SECRET'ом.
"""
import asyncio
import base64
import hashlib
import hmac
import os
import sqlite3

from sqlalchemy import select
from vera_shared.crypto import encrypt
from vera_shared.db.engine import get_session, init_engine
from vera_shared.db.models import Base
from vera_shared.db.models_sources import GmailAccountRow


def _vera2_derive_key(master: str) -> bytes:
    """Vera 2 ключ: SHA256("vera-token-enc-v1|" + master)."""
    return hashlib.sha256(b"vera-token-enc-v1|" + master.encode()).digest()


def decrypt_vera2(stored: str, master_secret: str) -> str:
    """Расшифровка формата Vera 2.0 (AES-256-CTR + HMAC-SHA256)."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    if not stored.startswith("enc1:"):
        return stored
    raw = base64.urlsafe_b64decode(stored[5:].encode("ascii"))
    iv, mac, ct = raw[:16], raw[16:32], raw[32:]
    key = _vera2_derive_key(master_secret)
    expected = hmac.new(key, iv + ct, hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(mac, expected):
        raise ValueError("hmac mismatch")
    decryptor = Cipher(algorithms.AES(key), modes.CTR(iv)).decryptor()
    return (decryptor.update(ct) + decryptor.finalize()).decode("utf-8")


async def main():
    v2_secret = os.environ["VERA2_SECRET"]
    v3_secret = os.environ["VERA3_SECRET"]
    sqlite_path = os.environ.get("VERA2_SQLITE", "/backup/vera2_backup.db")

    engine = await init_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute("SELECT * FROM gmail_accounts WHERE is_active=1"))

    migrated = 0
    for r in rows:
        try:
            refresh_plain = decrypt_vera2(r["refresh_token_enc"], v2_secret)
        except Exception as e:
            print(f"DECRYPT FAILED for {r['email']}: {e}")
            continue
        refresh_v3 = encrypt(refresh_plain, v3_secret)

        async with get_session() as s:
            existing = (await s.execute(
                select(GmailAccountRow).where(GmailAccountRow.email == r["email"])
            )).scalar_one_or_none()
            if existing:
                print(f"SKIP (already exists): {r['email']}")
                continue
            s.add(GmailAccountRow(
                email=r["email"],
                refresh_token_enc=refresh_v3,
                is_active=True,
            ))
            migrated += 1
            print(f"OK: {r['email']}")
    print(f"Total migrated: {migrated}")


if __name__ == "__main__":
    asyncio.run(main())
