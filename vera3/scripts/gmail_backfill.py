"""Gmail backfill — тянет ВСЕ письма за период для всех активных аккаунтов.

Использует тот же refresh-token flow что и обычный poller, но идёт глубже
в историю с пагинацией.

Запуск:
    docker run --rm --network host \\
      -v /var/www/vera3:/work \\
      -e GMAIL_CLIENT_ID=... \\
      -e GMAIL_CLIENT_SECRET=... \\
      -e TOKEN_SECRET=... \\
      -e DATABASE_URL=... \\
      -e BACKFILL_DAYS=30 \\
      -w /work python:3.12-slim sh -c \\
      'pip install -q -e ./shared asyncpg httpx; PYTHONPATH=shared python scripts/gmail_backfill.py'
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, "/app/src")
sys.path.insert(0, "/work/services/ingestor-gmail/src")

import httpx
from sqlalchemy import select, update

from vera_shared.db.engine import get_session, init_engine
from vera_shared.db.models import EventRow
from vera_shared.db.models_sources import GmailAccountRow
from vera_shared.crypto import decrypt

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("gmail-backfill")

CLIENT_ID = os.environ["GMAIL_CLIENT_ID"]
CLIENT_SECRET = os.environ["GMAIL_CLIENT_SECRET"]
DAYS_BACK = int(os.environ.get("BACKFILL_DAYS", "30"))
MAX_PER_BATCH = 100
PACE_BETWEEN_REQ_S = 0.1


async def refresh_access(refresh_token: str) -> str:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            "https://oauth2.googleapis.com/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
            },
        )
    r.raise_for_status()
    return r.json()["access_token"]


async def fetch_list_page(
    access_token: str, query: str, page_token: str | None,
) -> tuple[list[str], str | None]:
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {"q": query, "maxResults": MAX_PER_BATCH}
    if page_token:
        params["pageToken"] = page_token
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            params=params, headers=headers,
        )
        r.raise_for_status()
    data = r.json()
    ids = [m["id"] for m in data.get("messages", [])]
    return ids, data.get("nextPageToken")


async def fetch_message(access_token: str, mid: str) -> dict | None:
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{mid}",
            params={"format": "full"}, headers=headers,
        )
    if r.status_code == 200:
        return r.json()
    log.warning("get %s: HTTP %s", mid, r.status_code)
    return None


# Импорт _format_event из ingestor-gmail
from ingestor_gmail.poller import _format_event


async def backfill_account(acc: GmailAccountRow) -> int:
    """Backfill одного аккаунта за DAYS_BACK дней."""
    try:
        refresh = decrypt(acc.refresh_token_enc)
    except Exception as e:
        log.error("decrypt failed for %s: %s", acc.email, e)
        return 0
    try:
        access = await refresh_access(refresh)
    except Exception as e:
        log.error("refresh failed for %s: %s", acc.email, e)
        return 0

    # Build query
    start_date = (datetime.utcnow() - timedelta(days=DAYS_BACK)).strftime("%Y/%m/%d")
    query = f"after:{start_date}"
    log.info("Backfill %s with query: %s", acc.email, query)

    total_inserted = 0
    page_token: str | None = None
    page_n = 0

    while True:
        page_n += 1
        try:
            ids, page_token = await fetch_list_page(access, query, page_token)
        except Exception as e:
            log.error("list page %s failed: %s", page_n, e)
            break
        if not ids:
            break
        log.info("[%s] page %d: %d message ids, total inserted so far: %d",
                 acc.email, page_n, len(ids), total_inserted)

        for mid in ids:
            await asyncio.sleep(PACE_BETWEEN_REQ_S)
            # Skip if already in DB
            async with get_session() as s:
                exists = (await s.execute(
                    select(EventRow.id).where(
                        EventRow.source == "gmail",
                        EventRow.source_event_id == mid,
                    )
                )).scalar_one_or_none()
                if exists:
                    continue

            msg = await fetch_message(access, mid)
            if not msg:
                continue

            spec = _format_event(acc.email, msg)
            async with get_session() as s:
                s.add(EventRow(triage_status="pending", **spec))
                total_inserted += 1

        if not page_token:
            break

    log.info("DONE %s: %d new events", acc.email, total_inserted)
    async with get_session() as s:
        await s.execute(
            update(GmailAccountRow)
            .where(GmailAccountRow.id == acc.id)
            .values(last_polled_at=datetime.utcnow())
        )
    return total_inserted


async def main():
    await init_engine()
    log.info("Gmail backfill: %d days back", DAYS_BACK)
    async with get_session() as s:
        accs = (await s.execute(
            select(GmailAccountRow).where(GmailAccountRow.is_active.is_(True))
        )).scalars().all()
    log.info("Active accounts: %d", len(accs))

    grand_total = 0
    for acc in accs:
        try:
            n = await backfill_account(acc)
            grand_total += n
        except Exception as e:
            log.exception("Account %s failed: %s", acc.email, e)
        await asyncio.sleep(2)

    log.info("ALL DONE: %d total new events", grand_total)


if __name__ == "__main__":
    asyncio.run(main())
