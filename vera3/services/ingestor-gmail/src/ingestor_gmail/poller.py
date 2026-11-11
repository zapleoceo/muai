"""Gmail poller — раз в N сек тянет новые письма из каждого активного аккаунта.

Использует OAuth refresh_token (сохранены в gmail_accounts). Не требует
re-auth — Google продлевает access_token из refresh.

Транспорт — `gmail_api`, разбор письма — `message_parse`, SQL — `gmail_store`;
здесь курсор, дедуп по БД и сам прогон. Прежние имена (`refresh_access`,
`_format_event`, `correspondent_of`, …) реэкспортируются отсюда. В журнал
аккаунт попадает по id, не по адресу: адрес — персональные данные.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta

import httpx
from vera_shared.crypto import decrypt
from vera_shared.db.engine import init_engine
from vera_shared.db.models_sources import GmailAccountRow
from vera_shared.ingest import insert_events, sync_author_entities
from vera_shared.timeutil import utc_naive_now

from ingestor_gmail.gmail_api import (
    ScopeInsufficient,
    TokenRevoked,
    fetch_full_messages,
    fetch_message_ids,
    fetch_messages,
    refresh_access,
)
from ingestor_gmail.gmail_store import (
    load_known_message_ids,
    load_pollable_accounts,
    mark_account_failed,
    mark_account_polled,
)
from ingestor_gmail.message_parse import (
    _correspondent_entity_of,
    _extract_text,
    _format_event,
    _html_to_text,
    correspondent_of,
)

__all__ = [
    "ScopeInsufficient",
    "TokenRevoked",
    "_extract_text",
    "_format_event",
    "_html_to_text",
    "correspondent_of",
    "fetch_full_messages",
    "fetch_message_ids",
    "fetch_messages",
    "httpx",
    "refresh_access",
]

log = logging.getLogger("gmail")

POLL_S = int(os.environ.get("GMAIL_POLL_S", "300"))  # 5 минут
# Предохранитель на прогон (не лимит выборки — идём по всем страницам до него).
MAX_PER_RUN = int(os.environ.get("GMAIL_MAX_PER_RUN", "500"))
CURSOR_OVERLAP = timedelta(hours=int(os.environ.get("GMAIL_CURSOR_OVERLAP_H", "24")))

_NO_SCOPE_ERROR = ("Нет доступа к Gmail (403): при входе не выдан "
                   "scope чтения писем. Переподключи с полным доступом.")


async def filter_new_ids(all_ids: list[str], per_run: int) -> tuple[list[str], bool]:
    """Отсечь уже забранные id по БД. truncated=True — новых больше per_run,
    курсор last_polled_at в этот прогон двигать нельзя (хвост доберём потом)."""
    if not all_ids:
        return [], False
    known = await load_known_message_ids(all_ids)
    new_ids = [i for i in all_ids if i not in known]
    return new_ids, len(new_ids) > per_run


def build_query(last_polled_at: datetime | None) -> str:
    """Поисковый запрос листинга.

    `after:YYYY/MM/DD` — календарные сутки, и Gmail считает их не в UTC, в
    котором хранится last_polled_at: письмо, пришедшее между UTC-полуночью и
    полуночью по Gmail, после опроса в первые часы UTC-суток выпадало из
    `after:` навсегда. Unix-время в секундах точное; отступ в CURSOR_OVERLAP
    покрывает письма, проиндексированные с опозданием (дубли отсекает
    filter_new_ids по БД).
    """
    if last_polled_at is None:
        return "newer_than:7d"
    since = last_polled_at.replace(tzinfo=UTC) - CURSOR_OVERLAP
    return f"after:{int(since.timestamp())}"


async def _access_token(acc: GmailAccountRow) -> str | None:
    """Свежий access-токен аккаунта; None — прогон пропускаем, статус записан."""
    try:
        refresh = decrypt(acc.refresh_token_enc)
    except Exception as e:
        log.error("Decrypt failed for account %s: %s", acc.id, e)
        return None
    try:
        tok = await refresh_access(refresh)
    except TokenRevoked as e:
        # Токен мёртв — помечаем needs_reauth, поллер перестанет долбиться.
        # Восстановление: Дима жмёт «Переподключить» в дашборде.
        log.warning("Token REVOKED for account %s — needs re-auth: %s", acc.id, e)
        await mark_account_failed(acc.id, str(e)[:500], needs_reauth=True)
        return None
    except Exception as e:
        log.error("Refresh failed for account %s: %s", acc.id, e)
        await mark_account_failed(acc.id, str(e)[:500], needs_reauth=False)
        return None
    return tok["access_token"]


async def _fetch_new(acc: GmailAccountRow, access_token: str) -> tuple[list[dict], bool] | None:
    """(новые письма, truncated); None — выборка упала, статус записан."""
    query = build_query(acc.last_polled_at)
    try:
        # id-only list дёшев — смотрим весь бэклог (до 10×MAX_PER_RUN), дедупим
        # по БД и full-GET'им только новые. Раньше при бэклоге >MAX_PER_RUN
        # забирались 500 НОВЕЙШИХ, курсор прыгал на сегодня и старый хвост
        # терялся навсегда (after: — date-granular).
        all_ids = await fetch_message_ids(access_token, query, cap=MAX_PER_RUN * 10)
        new_ids, truncated = await filter_new_ids(all_ids, MAX_PER_RUN)
        if truncated:
            log.warning("gmail/account %s: backlog %d new — беру %d, курсор не двигаю",
                        acc.id, len(new_ids), MAX_PER_RUN)
        messages = await fetch_full_messages(access_token, new_ids[:MAX_PER_RUN])
    except ScopeInsufficient as e:
        log.warning("Scope INSUFFICIENT for account %s — re-auth with Gmail access: %s",
                    acc.id, e)
        await mark_account_failed(acc.id, _NO_SCOPE_ERROR, needs_reauth=True)
        return None
    except Exception as e:
        log.error("Fetch failed for account %s: %s", acc.id, e)
        await mark_account_failed(acc.id, str(e)[:500], needs_reauth=False)
        return None
    return messages, truncated


async def poll_account(acc: GmailAccountRow) -> int:
    """Poll one account. Returns count of inserted events."""
    access_token = await _access_token(acc)
    if access_token is None:
        return 0
    fetched = await _fetch_new(acc, access_token)
    if fetched is None:
        return 0
    messages, truncated = fetched

    specs = [_format_event(acc.email, msg) for msg in messages]
    # Вставка и дедуп — общим ядром (ON CONFLICT), а не местным
    # SELECT-then-INSERT: gmail_backfill.py пишет тем же способом параллельно.
    fresh = await insert_events(specs)
    inserted = len(fresh)
    # Identity graph: собеседник каждого нового письма → сущность с alias
    # (gmail, email). Один upsert на адрес за прогон.
    await sync_author_entities(fresh, source="gmail",
                               author_of=_correspondent_entity_of(acc.email))

    # При truncated курсор НЕ двигаем: следующий прогон снова листит от
    # старой даты, пропускает уже забранное по БД и добирает хвост.
    await mark_account_polled(acc.id, utc_naive_now(), advance_cursor=not truncated)

    if inserted:
        log.info("gmail/account %s: %s new events", acc.id, inserted)
    return inserted


async def main_loop() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await init_engine()
    log.info("Gmail poller starting, poll=%ss", POLL_S)

    while True:
        try:
            for acc in await load_pollable_accounts():
                try:
                    await poll_account(acc)
                except Exception as e:
                    log.exception("Account %s polling failed: %s", acc.id, e)
                await asyncio.sleep(2)  # rate-friendly

        except Exception as e:
            log.exception("Outer loop error: %s", e)

        await asyncio.sleep(POLL_S)


if __name__ == "__main__":
    asyncio.run(main_loop())
