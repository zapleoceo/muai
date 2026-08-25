"""Обход действий: пагинация вглубь, курсор, дедуп вставки."""
from __future__ import annotations

import os
import sys

import pytest
from sqlalchemy import select

sys.path.insert(0, os.path.join(
    os.path.dirname(__file__), "..", "..",
    "services", "ingestor-trello", "src"))

os.environ.setdefault("TRELLO_API_KEY", "k")
os.environ.setdefault("TRELLO_TOKEN", "t")

from ingestor_trello import poller, store  # noqa: E402

ME = "me-member-id"


class _FakeClient:
    """Отдаёт заранее нарезанные страницы, запоминая параметры вызовов."""

    def __init__(self, pages: list[list[dict]]):
        self._pages = pages
        self.calls: list[dict] = []

    async def list_actions(self, board_id, *, since=None, before=None, limit=1000):
        self.calls.append({"since": since, "before": before})
        return self._pages.pop(0) if self._pages else []


def _actions(prefix: str, n: int) -> list[dict]:
    """Новые первыми — как отдаёт Trello."""
    return [{
        "id": f"{prefix}{i}",
        "type": "createCard",
        "date": "2026-08-24T10:00:00.000Z",
        "idMemberCreator": ME,
        "memberCreator": {"id": ME, "username": "dima", "fullName": "Dima"},
        "data": {"card": {"name": f"Карточка {prefix}{i}"}, "board": {"name": "Доска"}},
    } for i in range(n)]


@pytest.mark.asyncio
async def test_short_page_completes_immediately(monkeypatch):
    monkeypatch.setattr(poller, "ACTIONS_PAGE", 3)
    client = _FakeClient([_actions("a", 2)])
    actions, complete = await poller.fetch_new_actions(client, "b1", "cursor-0")
    assert complete is True
    assert len(actions) == 2
    assert client.calls == [{"since": "cursor-0", "before": None}]


@pytest.mark.asyncio
async def test_full_page_walks_deeper_with_before(monkeypatch):
    monkeypatch.setattr(poller, "ACTIONS_PAGE", 3)
    client = _FakeClient([_actions("a", 3), _actions("b", 1)])
    actions, complete = await poller.fetch_new_actions(client, "b1", "cursor-0")
    assert complete is True
    assert len(actions) == 4
    # Вторая страница идёт от самого старого действия первой.
    assert client.calls[1] == {"since": "cursor-0", "before": "a2"}


@pytest.mark.asyncio
async def test_backlog_deeper_than_cap_is_reported_incomplete(monkeypatch):
    monkeypatch.setattr(poller, "ACTIONS_PAGE", 2)
    monkeypatch.setattr(poller, "MAX_PAGES", 3)
    client = _FakeClient([_actions("a", 2), _actions("b", 2), _actions("c", 2)])
    actions, complete = await poller.fetch_new_actions(client, "b1", "cursor-0")
    assert complete is False
    assert len(actions) == 6


@pytest.mark.usefixtures("sqlite_db")
class TestBoardState:

    async def _board(self, board_id="b1", cursor=None):
        from vera_shared.db.engine import get_session
        from vera_shared.db.models_sources import TrelloBoardRow
        async with get_session() as s:
            s.add(TrelloBoardRow(board_id=board_id, name="Доска",
                                 last_action_id=cursor, is_active=True))
        async with get_session() as s:
            return (await s.execute(
                select(TrelloBoardRow).where(TrelloBoardRow.board_id == board_id)
            )).scalar_one()

    async def _cursor(self, board_id="b1"):
        from vera_shared.db.engine import get_session
        from vera_shared.db.models_sources import TrelloBoardRow
        async with get_session() as s:
            return (await s.execute(
                select(TrelloBoardRow.last_action_id)
                .where(TrelloBoardRow.board_id == board_id)
            )).scalar_one()

    @pytest.mark.asyncio
    async def test_cursor_moves_to_newest_action(self, monkeypatch):
        monkeypatch.setattr(poller, "ACTIONS_PAGE", 5)
        row = await self._board(cursor="old")
        client = _FakeClient([_actions("a", 3)])
        inserted = await poller.poll_board(client, row, ME, "dima")
        assert inserted == 3
        assert await self._cursor() == "a0"

    @pytest.mark.asyncio
    async def test_incomplete_backlog_keeps_cursor(self, monkeypatch):
        monkeypatch.setattr(poller, "ACTIONS_PAGE", 2)
        monkeypatch.setattr(poller, "MAX_PAGES", 1)
        row = await self._board(board_id="b2", cursor="old")
        client = _FakeClient([_actions("a", 2)])
        await poller.poll_board(client, row, ME, "dima")
        # Хвост не разобран — курсор остаётся, иначе середина потеряется молча.
        assert await self._cursor("b2") == "old"

    @pytest.mark.asyncio
    async def test_reingesting_same_actions_inserts_nothing(self, monkeypatch):
        monkeypatch.setattr(poller, "ACTIONS_PAGE", 5)
        row = await self._board(board_id="b3", cursor="old")
        first = await poller.poll_board(_FakeClient([_actions("z", 2)]), row, ME, "dima")
        again = await poller.poll_board(_FakeClient([_actions("z", 2)]), row, ME, "dima")
        assert (first, again) == (2, 0)

    @pytest.mark.asyncio
    async def test_closed_board_is_deactivated_not_deleted(self):
        from vera_shared.db.engine import get_session
        from vera_shared.db.models_sources import TrelloBoardRow
        await store.upsert_boards([{"id": "b9", "name": "Живая"},
                                   {"id": "b8", "name": "Закроется"}])
        active = await store.upsert_boards([{"id": "b9", "name": "Живая"}])
        assert [r.board_id for r in active] == ["b9"]
        async with get_session() as s:
            gone = (await s.execute(
                select(TrelloBoardRow).where(TrelloBoardRow.board_id == "b8")
            )).scalar_one()
        assert gone.is_active is False
