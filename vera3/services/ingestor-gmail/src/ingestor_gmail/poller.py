"""Gmail poller — раз в N сек тянет новые письма из каждого активного аккаунта.

Использует OAuth refresh_token (сохранены в gmail_accounts). Не требует
re-auth — Google продлевает access_token из refresh.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from sqlalchemy import select, update
from vera_shared.crypto import decrypt
from vera_shared.db.engine import get_session, init_engine
from vera_shared.db.models import EventRow
from vera_shared.db.models_sources import GmailAccountRow
from vera_shared.graph.identity import entity_kind_for_email
from vera_shared.ingest import AuthorExtractor, insert_events, sync_author_entities
from vera_shared.timeutil import utc_naive_now

log = logging.getLogger("gmail")

CLIENT_ID = os.environ["GMAIL_CLIENT_ID"]
CLIENT_SECRET = os.environ["GMAIL_CLIENT_SECRET"]
POLL_S = int(os.environ.get("GMAIL_POLL_S", "300"))  # 5 минут
# Предохранитель на прогон (не лимит выборки — идём по всем страницам до него).
MAX_PER_RUN = int(os.environ.get("GMAIL_MAX_PER_RUN", "500"))


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


async def filter_new_ids(all_ids: list[str], per_run: int) -> tuple[list[str], bool]:
    """Отсечь уже забранные id по БД. truncated=True — новых больше per_run,
    курсор last_polled_at в этот прогон двигать нельзя (хвост доберём потом)."""
    if not all_ids:
        return [], False
    async with get_session() as s:
        known = set((await s.execute(
            select(EventRow.source_event_id).where(
                EventRow.source == "gmail",
                EventRow.source_event_id.in_(all_ids),
            )
        )).scalars().all())
    new_ids = [i for i in all_ids if i not in known]
    return new_ids, len(new_ids) > per_run


async def fetch_messages(access_token: str, query: str, max_total: int = 500) -> list[dict]:
    """List + get полных messages. Оставлен для совместимости (тесты статусов);
    боевой путь в poll_account() дедупит id по БД между list и get."""
    ids = await fetch_message_ids(access_token, query, cap=max_total)
    return await fetch_full_messages(access_token, ids)


_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _html_to_text(html: str) -> str:
    """HTML → text. Убирает <script>/<style> ПОЛНОСТЬЮ (содержимое и теги).

    Старый regex `<[^>]+>` оставлял JS-код в тексте — попадал в LLM, тратил
    токены и мог стать prompt injection через email-newsletter с JS внутри.
    """
    html = _SCRIPT_STYLE_RE.sub(" ", html)
    text = _TAG_RE.sub(" ", html)
    text = (text
            .replace("&nbsp;", " ").replace("&amp;", "&")
            .replace("&lt;", "<").replace("&gt;", ">")
            .replace("&quot;", '"').replace("&#39;", "'"))
    return _WS_RE.sub(" ", text).strip()


def _extract_text(payload: dict) -> str:
    """Recursive extract plain text from MIME parts."""
    if payload.get("mimeType") == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            try:
                return base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="ignore")
            except Exception:
                return ""
    for part in payload.get("parts", []) or []:
        txt = _extract_text(part)
        if txt:
            return txt
    if payload.get("mimeType") == "text/html":
        data = payload.get("body", {}).get("data", "")
        if data:
            try:
                html = base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="ignore")
                return _html_to_text(html)
            except Exception:
                return ""
    return ""


def correspondent_of(account_email: str, from_: str, to_: str) -> tuple[str, str] | None:
    """(email, display_name) собеседника письма — то, что станет person-сущностью.

    received → From; sent → первый адрес To. Свои ящики пропускаем (иначе
    каждый свой аккаунт превратился бы в отдельного «человека»)."""
    from email.utils import getaddresses, parseaddr
    if account_email.lower() in from_.lower():
        pairs = getaddresses([to_]) if to_ else []
        name, addr = pairs[0] if pairs else ("", "")
    else:
        name, addr = parseaddr(from_)
    addr = (addr or "").strip().lower()
    if not addr or "@" not in addr or addr == account_email.lower():
        return None
    return addr, (name or "").strip() or addr.split("@")[0]


def _correspondent_entity_of(account_email: str) -> AuthorExtractor:
    """Экстрактор для `ingest.sync_author_entities`: письмо → сущность автора.

    Служебные ящики (no-reply@, invoice@, crm@) заводятся организациями, а не
    людьми: иначе граф людей засоряется, а одна компания с нескольких адресов
    выглядит как несколько одноимённых «персон». См. identity.py.
    """
    def extract(spec: dict[str, Any]) -> dict[str, Any] | None:
        meta = spec.get("metadata_") or {}
        who = correspondent_of(account_email, meta.get("from", ""), meta.get("to", ""))
        if who is None:
            return None
        addr, display = who
        return {
            "type": entity_kind_for_email(addr),
            "name": display,
            "identifier": addr,
            "display_name": display,
            "attributes": {"email": addr},
        }

    return extract


def _format_event(account_email: str, msg: dict) -> dict[str, Any]:
    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    from_ = headers.get("from", "")
    to_ = headers.get("to", "")
    subject = headers.get("subject", "(no subject)")
    date_raw = headers.get("date", "")

    try:
        occurred = parsedate_to_datetime(date_raw)
    except Exception:
        occurred = utc_naive_now()
    if occurred.tzinfo:
        # Convert to UTC FIRST, then strip tz — иначе "11:22 +07:00" станет "11:22"
        # naive вместо корректного "04:22" UTC.
        from datetime import timezone
        occurred = occurred.astimezone(timezone.utc).replace(tzinfo=None)

    direction = "sent" if account_email.lower() in from_.lower() else "received"
    author_role = "self" if direction == "sent" else "counterparty"
    author_label = "Я" if author_role == "self" else (from_ or "(unknown)")
    body = _extract_text(msg.get("payload", {}))[:8000]

    content = (
        f"Author: {author_label} [{author_role}]\n"
        f"From: {from_}\n"
        f"To: {to_}\n"
        f"Subject: {subject}\n"
        f"Date: {date_raw}\n"
        f"Direction: {direction}\n"
        f"---\n{body}"
    )

    return {
        "source": "gmail",
        "source_event_id": msg["id"],
        "account": account_email,
        "category": "email",
        "content_text": content,
        "occurred_at": occurred,
        "metadata_": {
            "direction": direction,
            "author_role": author_role,
            "author_label": author_label,
            "subject": subject,
            "from": from_,
            "to": to_,
        },
    }


async def poll_account(acc: GmailAccountRow) -> int:
    """Poll one account. Returns count of inserted events."""
    # Decrypt refresh token
    try:
        refresh = decrypt(acc.refresh_token_enc)
    except Exception as e:
        log.error("Decrypt failed for %s: %s", acc.email, e)
        return 0

    # Refresh access token if needed
    try:
        tok = await refresh_access(refresh)
    except TokenRevoked as e:
        # Токен мёртв — помечаем needs_reauth, поллер перестанет долбиться.
        # Восстановление: Дима жмёт «Переподключить» в дашборде.
        log.warning("Token REVOKED for %s — needs re-auth: %s", acc.email, e)
        async with get_session() as s:
            await s.execute(
                update(GmailAccountRow).where(GmailAccountRow.id == acc.id)
                .values(needs_reauth=True, last_error=str(e)[:500])
            )
        return 0
    except Exception as e:
        log.error("Refresh failed for %s: %s", acc.email, e)
        async with get_session() as s:
            await s.execute(
                update(GmailAccountRow).where(GmailAccountRow.id == acc.id)
                .values(last_error=str(e)[:500])
            )
        return 0
    access_token = tok["access_token"]

    # Build query: newer_than 7d на старте, потом — с last_polled
    if acc.last_polled_at:
        # Gmail accepts dates like 2024/01/15
        ds = acc.last_polled_at.strftime("%Y/%m/%d")
        query = f"after:{ds}"
    else:
        query = "newer_than:7d"

    try:
        # id-only list дёшев — смотрим весь бэклог (до 10×MAX_PER_RUN), дедупим
        # по БД и full-GET'им только новые. Раньше при бэклоге >MAX_PER_RUN
        # забирались 500 НОВЕЙШИХ, курсор прыгал на сегодня и старый хвост
        # терялся навсегда (after: — date-granular).
        all_ids = await fetch_message_ids(access_token, query, cap=MAX_PER_RUN * 10)
        new_ids, truncated = await filter_new_ids(all_ids, MAX_PER_RUN)
        if truncated:
            log.warning("gmail/%s: backlog %d new — беру %d, курсор не двигаю",
                        acc.email, len(new_ids), MAX_PER_RUN)
        messages = await fetch_full_messages(access_token, new_ids[:MAX_PER_RUN])
    except ScopeInsufficient as e:
        log.warning("Scope INSUFFICIENT for %s — re-auth with Gmail access: %s",
                    acc.email, e)
        async with get_session() as s:
            await s.execute(
                update(GmailAccountRow).where(GmailAccountRow.id == acc.id)
                .values(needs_reauth=True,
                        last_error="Нет доступа к Gmail (403): при входе не выдан "
                                   "scope чтения писем. Переподключи с полным доступом.")
            )
        return 0
    except Exception as e:
        log.error("Fetch failed for %s: %s", acc.email, e)
        async with get_session() as s:
            await s.execute(
                update(GmailAccountRow).where(GmailAccountRow.id == acc.id)
                .values(last_error=str(e)[:500])
            )
        return 0

    specs = [_format_event(acc.email, msg) for msg in messages]
    # Вставка и дедуп — общим ядром (ON CONFLICT), а не местным
    # SELECT-then-INSERT: gmail_backfill.py пишет тем же способом параллельно.
    fresh = await insert_events(specs)
    inserted = len(fresh)
    # Identity graph: собеседник каждого нового письма → сущность с alias
    # (gmail, email). Один upsert на адрес за прогон.
    await sync_author_entities(fresh, source="gmail",
                               author_of=_correspondent_entity_of(acc.email))

    now = utc_naive_now()
    async with get_session() as s:
        # При truncated курсор НЕ двигаем: следующий прогон снова листит от
        # старой даты, пропускает уже забранное по БД и добирает хвост.
        values: dict[str, Any] = {"last_ok_at": now,
                                  "needs_reauth": False, "last_error": None}
        if not truncated:
            values["last_polled_at"] = now
        await s.execute(
            update(GmailAccountRow)
            .where(GmailAccountRow.id == acc.id)
            .values(**values)
        )

    if inserted:
        log.info("gmail/%s: %s new events", acc.email, inserted)
    return inserted


async def main_loop():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await init_engine()
    log.info("Gmail poller starting, poll=%ss", POLL_S)

    while True:
        try:
            async with get_session() as s:
                # Пропускаем needs_reauth — токен мёртв, refresh бесполезен,
                # вернётся в опрос после переподключения через дашборд.
                accs = (await s.execute(
                    select(GmailAccountRow)
                    .where(GmailAccountRow.is_active.is_(True))
                    .where(GmailAccountRow.needs_reauth.is_(False))
                )).scalars().all()

            for acc in accs:
                try:
                    await poll_account(acc)
                except Exception as e:
                    log.exception("Account %s polling failed: %s", acc.email, e)
                await asyncio.sleep(2)  # rate-friendly

        except Exception as e:
            log.exception("Outer loop error: %s", e)

        await asyncio.sleep(POLL_S)


if __name__ == "__main__":
    asyncio.run(main_loop())
