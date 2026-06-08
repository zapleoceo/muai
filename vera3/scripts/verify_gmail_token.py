"""Тест что Vera 2.0 → Vera 3.0 decrypt дал валидный Gmail refresh token."""
import asyncio
import os
import httpx

from vera_shared.db.engine import get_session, init_engine
from vera_shared.db.models_sources import GmailAccountRow
from vera_shared.tokens.crypto import decrypt
from sqlalchemy import select


async def main():
    await init_engine()
    async with get_session() as s:
        accs = (await s.execute(select(GmailAccountRow))).scalars().all()

    for acc in accs:
        refresh = decrypt(acc.refresh_token_enc)
        print(f"{acc.email}: token starts={refresh[:8]} len={len(refresh)}")
        # Try refresh
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                    "client_id": os.environ["GMAIL_CLIENT_ID"],
                    "client_secret": os.environ["GMAIL_CLIENT_SECRET"],
                },
            )
        print(f"  → HTTP {r.status_code}: {r.text[:200]}")


if __name__ == "__main__":
    asyncio.run(main())
