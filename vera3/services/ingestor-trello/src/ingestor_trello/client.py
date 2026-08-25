"""HTTP-клиент Trello. Только транспорт: ключи, ретраи, разбор ответа."""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

log = logging.getLogger("trello")

API = "https://api.trello.com/1"
TIMEOUT = 30
RETRIES = 3
# Trello отдаёт максимум 1000 действий за запрос, дальше — только пагинацией.
ACTIONS_PAGE = 1000


class TrelloAuthError(Exception):
    """Ключ или токен не приняты (401/403) — дальше долбиться бессмысленно."""


class TrelloClient:
    def __init__(self, api_key: str | None = None, token: str | None = None):
        self.api_key = api_key if api_key is not None else os.environ.get("TRELLO_API_KEY", "")
        self.token = token if token is not None else os.environ.get("TRELLO_TOKEN", "")
        if not self.api_key or not self.token:
            raise TrelloAuthError("TRELLO_API_KEY / TRELLO_TOKEN не заданы")

    async def _get(self, path: str, **params: Any) -> Any:
        params.update(key=self.api_key, token=self.token)
        last: Exception | None = None
        for attempt in range(RETRIES):
            try:
                async with httpx.AsyncClient(timeout=TIMEOUT) as c:
                    r = await c.get(f"{API}{path}", params=params)
                if r.status_code in (401, 403):
                    raise TrelloAuthError(f"{r.status_code}: {r.text[:200]}")
                if r.status_code == 429:
                    # Лимит 300 req/10s на ключ. При опросе раз в 5 минут сюда
                    # можно попасть только на первой раскрутке большой доски.
                    wait = 2 ** attempt
                    log.warning("trello: 429, пауза %sс", wait)
                    await asyncio.sleep(wait)
                    continue
                r.raise_for_status()
                return r.json()
            except TrelloAuthError:
                raise
            except (httpx.HTTPError, ValueError) as e:
                last = e
                await asyncio.sleep(2 ** attempt)
        raise ConnectionError(f"trello {path}: ретраи исчерпаны ({last})")

    async def whoami(self) -> dict[str, Any]:
        return await self._get("/members/me", fields="id,username,fullName")

    async def list_boards(self) -> list[dict[str, Any]]:
        return await self._get("/members/me/boards", filter="open", fields="name,closed")

    async def list_actions(
        self, board_id: str, *, since: str | None = None,
        before: str | None = None, limit: int = ACTIONS_PAGE,
    ) -> list[dict[str, Any]]:
        """Действия доски новее курсора, от новых к старым.

        since — id действия либо ISO-дата; before — пагинация вглубь окна."""
        params: dict[str, Any] = {
            "limit": min(limit, ACTIONS_PAGE),
            "memberCreator": "true",
            "memberCreator_fields": "id,username,fullName",
        }
        if since:
            params["since"] = since
        if before:
            params["before"] = before
        return await self._get(f"/boards/{board_id}/actions", **params)

    async def list_open_cards(self, board_id: str) -> list[dict[str, Any]]:
        return await self._get(
            f"/boards/{board_id}/cards",
            filter="open",
            fields="name,due,dueComplete,shortLink,idList",
        )

