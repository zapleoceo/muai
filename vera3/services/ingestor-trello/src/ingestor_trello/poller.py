"""Trello poller — раз в N секунд забирает новые действия каждой доски.

Курсор — id последнего действия, а не дата: Trello отдаёт действия от новых
к старым, и id-курсор не теряет хвост при всплеске активности (та же грабля,
что чинили у gmail с date-granular `after:`).
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import timedelta

from vera_shared.db.engine import init_engine
from vera_shared.db.models_sources import TrelloBoardRow
from vera_shared.ingest import poll_forever
from vera_shared.timeutil import utc_naive_now

from ingestor_trello import digest, store
from ingestor_trello.client import ACTIONS_PAGE, TrelloAuthError, TrelloClient
from ingestor_trello.mapper import action_to_event

log = logging.getLogger("trello")

POLL_S = int(os.environ.get("TRELLO_POLL_S", "300"))
BOOTSTRAP_DAYS = int(os.environ.get("TRELLO_BOOTSTRAP_DAYS", "7"))
# Потолок обхода за один прогон: 20 × 1000 действий. Упереться можно только на
# первой раскрутке очень живой доски — тогда курсор не двигаем и добираем позже.
MAX_PAGES = int(os.environ.get("TRELLO_MAX_PAGES", "20"))


async def fetch_new_actions(
    client: TrelloClient, board_id: str, since: str,
) -> tuple[list[dict], bool]:
    """Все действия новее курсора. Второй элемент — дошли ли до курсора."""
    collected: list[dict] = []
    before: str | None = None
    for _ in range(MAX_PAGES):
        page = await client.list_actions(board_id, since=since, before=before)
        collected.extend(page)
        if len(page) < ACTIONS_PAGE:
            return collected, True
        before = str(page[-1]["id"])
    return collected, False


async def poll_board(
    client: TrelloClient, row: TrelloBoardRow, me_id: str, me_username: str,
) -> int:
    since = row.last_action_id or (
        utc_naive_now() - timedelta(days=BOOTSTRAP_DAYS)
    ).isoformat()

    try:
        actions, complete = await fetch_new_actions(client, row.board_id, since)
    except TrelloAuthError:
        raise
    except Exception as e:
        log.error("trello/%s: не забрал действия: %s", row.name, e)
        await store.save_cursor(row.board_id, None, str(e)[:500])
        return 0

    specs = []
    for action in actions:
        spec = action_to_event(
            action, me_id=me_id, me_username=me_username, board_name=row.name,
        )
        if spec:
            specs.append(spec)

    fresh = await store.save_events(specs)

    if not complete:
        log.warning("trello/%s: бэклог глубже %d страниц — курсор не двигаю",
                    row.name, MAX_PAGES)
    cursor = str(actions[0]["id"]) if actions and complete else None
    await store.save_cursor(row.board_id, cursor, None)

    if fresh:
        log.info("trello/%s: %d новых событий", row.name, len(fresh))
    return len(fresh)


async def run_digest(
    client: TrelloClient, boards: list[TrelloBoardRow], me_username: str,
) -> None:
    now = utc_naive_now()
    if not await digest.due_today(now):
        return
    per_board = []
    for row in boards:
        try:
            per_board.append((row.name, await client.list_open_cards(row.board_id)))
        except Exception as e:
            log.warning("trello/%s: карточки для дайджеста не забрал: %s", row.name, e)
    built = digest.build_digest(per_board, now)
    if built:
        text, total, overdue = built
        await store.save_events(
            [digest.digest_event(text, total, overdue, now, me_username)]
        )
        log.info("trello: дайджест — %d карточек со сроками, %d просрочено",
                 total, overdue)
    await digest.mark_done(now)


class _Session:
    """Клиент и «кто я». Пересобирается после отказа в доступе — токен могли
    отозвать или он ещё не появился в .env."""

    def __init__(self) -> None:
        self.client: TrelloClient | None = None
        self.me_id = ""
        self.me_username = ""

    async def connect(self) -> TrelloClient:
        if self.client is None:
            self.client = TrelloClient()
            me = await self.client.whoami()
            self.me_id = str(me["id"])
            self.me_username = str(me.get("username") or "me")
            log.info("Trello: %s (%s), опрос каждые %sс",
                     self.me_username, self.me_id, POLL_S)
        return self.client

    def reset(self) -> None:
        self.client = None


async def main_loop() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await init_engine()
    session = _Session()

    async def poll_once() -> None:
        try:
            client = await session.connect()
            boards = await store.upsert_boards(await client.list_boards())
            for row in boards:
                await poll_board(client, row, session.me_id, session.me_username)
                await asyncio.sleep(1)
            await run_digest(client, boards, session.me_username)
        except TrelloAuthError:
            session.reset()
            raise

    await poll_forever(name="trello", poll_once=poll_once, interval_s=POLL_S,
                       auth_error=TrelloAuthError, log=log)


if __name__ == "__main__":
    asyncio.run(main_loop())
