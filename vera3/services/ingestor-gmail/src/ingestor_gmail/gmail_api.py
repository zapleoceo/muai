"""HTTP-транспорт Gmail: обновление access-токена и выборка писем."""
from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger("gmail")

CLIENT_ID = os.environ["GMAIL_CLIENT_ID"]
CLIENT_SECRET = os.environ["GMAIL_CLIENT_SECRET"]


class TokenRevoked(Exception):
    """Refresh-токен отозван Google (invalid_grant) — нужен повторный consent."""


class ScopeInsufficient(Exception):
    """Токен валиден, но без Gmail-scope (403). При consent сняли галку
    «Чтение писем» → нужен повторный consent с полным доступом."""


async def refresh_access(refresh_token: str) -> dict:
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
    if r.status_code == 400 and "invalid_grant" in r.text:
        raise TokenRevoked(r.text[:200])
    r.raise_for_status()
    return r.json()


async def fetch_message_ids(access_token: str, query: str, cap: int) -> list[str]:
    """List по filter, все страницы nextPageToken до cap. Дёшево (id-only) —
    cap намного выше MAX_PER_RUN, чтобы видеть весь бэклог и дедупить по БД
    ДО дорогих per-message GET."""
    headers = {"Authorization": f"Bearer {access_token}"}
    ids: list[str] = []
    async with httpx.AsyncClient(timeout=30) as c:
        page_token: str | None = None
        while len(ids) < cap:
            params: dict[str, str | int] = {"q": query, "maxResults": 100}
            if page_token:
                params["pageToken"] = page_token
            r = await c.get(
                "https://gmail.googleapis.com/gmail/v1/users/me/messages",
                params=params,
                headers=headers,
            )
            if r.status_code == 403 and "insufficient" in r.text.lower():
                raise ScopeInsufficient(r.text[:200])
            r.raise_for_status()
            body = r.json()
            ids.extend(m["id"] for m in body.get("messages", []))
            page_token = body.get("nextPageToken")
            if not page_token:
                break
    return ids[:cap]


async def fetch_full_messages(access_token: str, ids: list[str]) -> list[dict]:
    """GET format=full по каждому id (дорогая часть)."""
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=30) as c:
        messages = []
        failed: list[str] = []
        for mid in ids:
            # last_polled_at advances unconditionally after this run — a
            # message dropped here without a retry is lost for good (the
            # date-granular `after:` cursor won't re-fetch it later).
            # One immediate retry catches transient network blips.
            for attempt in range(2):
                try:
                    rm = await c.get(
                        f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{mid}",
                        params={"format": "full"},
                        headers=headers,
                    )
                    if rm.status_code == 200:
                        messages.append(rm.json())
                    else:
                        log.warning("Get message %s: HTTP %s", mid, rm.status_code)
                    break
                except httpx.HTTPError as e:
                    if attempt == 1:
                        failed.append(mid)
                        log.warning("Get message %s failed after retry: %s", mid, e)
        if failed:
            log.warning("gmail: %d message(s) dropped this run (fetch kept failing): %s",
                        len(failed), failed[:10])
    return messages


async def fetch_messages(access_token: str, query: str, max_total: int = 500) -> list[dict]:
    """List + get полных messages. Оставлен для совместимости (тесты статусов);
    боевой путь в poll_account() дедупит id по БД между list и get."""
    ids = await fetch_message_ids(access_token, query, cap=max_total)
    return await fetch_full_messages(access_token, ids)
