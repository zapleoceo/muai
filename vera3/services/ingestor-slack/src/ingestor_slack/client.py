"""HTTP-клиент Slack Web API. Только транспорт: токен, ретраи, пагинация.

Slack отвечает 200 даже на ошибку — беда лежит в теле как `{"ok": false,
"error": "..."}`. Поэтому статус-кода недостаточно, разбирать надо тело.

Лимиты: с 29.05.2025 `conversations.history` и `conversations.replies` у
приложений, распространяемых ВНЕ Marketplace, срезаны до 1 запроса в минуту и
15 объектов. Внутренние (custom) приложения своего же воркспейса под это НЕ
попадают — им остаются 50+ req/min и limit=1000. Наше приложение внутреннее и
не публикуется; если это когда-то изменится, опросная схема станет негодной и
переходить придётся на экспорт.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

log = logging.getLogger("slack")

API = "https://slack.com/api"
TIMEOUT = 30
RETRIES = 3
PAGE = 1000

#: `ok: false` с этими кодами ретраить бессмысленно — нужен новый токен.
AUTH_ERRORS = frozenset({
    "invalid_auth", "not_authed", "token_revoked", "token_expired",
    "account_inactive", "missing_scope", "no_permission", "org_login_required",
})


class SlackAuthError(Exception):
    """Токен не принят или ему не хватает прав."""


class SlackApiError(Exception):
    """`ok: false` по причине, которая может пройти сама."""


class SlackClient:
    def __init__(self, token: str | None = None):
        self.token = token if token is not None else os.environ.get("SLACK_USER_TOKEN", "")
        if not self.token:
            raise SlackAuthError(
                "токен не задан — подключи Slack в дашборде (/sources/slack) "
                "либо задай SLACK_USER_TOKEN")

    async def _call(self, method: str, **params: Any) -> dict[str, Any]:
        payload = {k: v for k, v in params.items() if v is not None}
        headers = {"Authorization": f"Bearer {self.token}"}
        last: Exception | None = None
        for attempt in range(RETRIES):
            try:
                async with httpx.AsyncClient(timeout=TIMEOUT) as c:
                    r = await c.get(f"{API}/{method}", params=payload, headers=headers)
                if r.status_code == 429:
                    wait = float(r.headers.get("Retry-After") or 2 ** attempt)
                    log.warning("slack/%s: 429, пауза %.0fс", method, wait)
                    await asyncio.sleep(wait)
                    continue
                r.raise_for_status()
                data = r.json()
            except (httpx.HTTPError, ValueError) as e:
                last = e
                await asyncio.sleep(2 ** attempt)
                continue

            if data.get("ok"):
                return data
            error = str(data.get("error") or "unknown")
            if error in AUTH_ERRORS:
                raise SlackAuthError(f"{method}: {error}")
            if error == "ratelimited":
                await asyncio.sleep(2 ** attempt)
                continue
            raise SlackApiError(f"{method}: {error}")
        raise ConnectionError(f"slack/{method}: ретраи исчерпаны ({last})")

    async def _paged(self, method: str, key: str, *, max_pages: int,
                     **params: Any) -> tuple[list[dict[str, Any]], bool]:
        """Собрать до max_pages страниц. Второй элемент — разобрано ли до конца."""
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(max_pages):
            data = await self._call(method, cursor=cursor, **params)
            items.extend(data.get(key) or [])
            cursor = ((data.get("response_metadata") or {}).get("next_cursor") or "").strip()
            if not cursor:
                return items, True
        return items, False

    async def whoami(self) -> dict[str, Any]:
        return await self._call("auth.test")

    async def list_conversations(self, *, max_pages: int = 10) -> list[dict[str, Any]]:
        items, _complete = await self._paged(
            "users.conversations", "channels", max_pages=max_pages,
            types="public_channel,private_channel,im,mpim",
            exclude_archived="true", limit=PAGE,
        )
        return items

    async def history(self, channel: str, *, oldest: str | None,
                      max_pages: int) -> tuple[list[dict[str, Any]], bool]:
        """Сообщения канала новее курсора, от новых к старым."""
        return await self._paged(
            "conversations.history", "messages", max_pages=max_pages,
            channel=channel, oldest=oldest, inclusive="false", limit=PAGE,
        )

    async def replies(self, channel: str, thread_ts: str, *, oldest: str | None,
                      max_pages: int) -> tuple[list[dict[str, Any]], bool]:
        """Ветка треда. Первый элемент — корневое сообщение."""
        return await self._paged(
            "conversations.replies", "messages", max_pages=max_pages,
            channel=channel, ts=thread_ts, oldest=oldest, limit=PAGE,
        )

    async def user_info(self, user_id: str) -> dict[str, Any]:
        return (await self._call("users.info", user=user_id)).get("user") or {}
