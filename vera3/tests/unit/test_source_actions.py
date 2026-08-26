"""Отключение источника: подтверждение обязательно, секрет остаётся, обратимо.

Отключение останавливает приём событий — по ссылке одним кликом такое делать
нельзя. И оно не должно ничего стирать: гасится строка, а не секрет и не
события.
"""
from __future__ import annotations

import base64
import os

os.environ.setdefault("TOKEN_SECRET", base64.urlsafe_b64encode(b"0" * 32).decode())
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("OWNER_TELEGRAM_ID", "169510539")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from unittest.mock import AsyncMock, patch  # noqa: E402

import pytest  # noqa: E402
from dashboard import source_state  # noqa: E402
from dashboard.app import app  # noqa: E402
from dashboard.auth import COOKIE_NAME  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from vera_shared.crypto import encrypt  # noqa: E402
from vera_shared.db.models import EventRow  # noqa: E402
from vera_shared.db.models_sources import SlackAuthRow  # noqa: E402

client = TestClient(app)


def _owner_cookie() -> str:
    from dashboard.auth_routes import _set_session_cookie
    from starlette.responses import Response
    resp = Response()
    _set_session_cookie(resp)
    header = resp.headers.get("set-cookie", "")
    return header.split(";")[0].split("=", 1)[1] if "=" in header else ""


class TestAuthGate:
    def test_confirm_requires_owner(self):
        assert client.get("/api/sources/slack/disconnect").status_code == 401

    def test_apply_requires_owner(self):
        assert client.post("/api/sources/slack/disconnect").status_code == 401


class TestConfirmation:
    def test_get_shows_what_exactly_will_stop_and_does_not_disconnect(self):
        """GET обязан только спрашивать. Если он гасит — источник отключится от
        любого превью ссылки."""
        state = source_state.State(True, "Sintegrum Team · dimondra",
                                   "опрос каналов и тредов остановится")
        with patch("dashboard.source_actions.state_of", AsyncMock(return_value=state)), \
             patch("dashboard.source_actions.disconnect", AsyncMock()) as killed:
            r = client.get("/api/sources/slack/disconnect",
                           cookies={COOKIE_NAME: _owner_cookie()})
        assert r.status_code == 200
        assert "Sintegrum Team · dimondra" in r.text
        assert "опрос каналов и тредов остановится" in r.text
        assert "останутся" in r.text          # про сохранность событий сказано
        killed.assert_not_awaited()

    def test_already_disconnected_just_goes_back(self):
        state = source_state.State(False, "токена нет")
        with patch("dashboard.source_actions.state_of", AsyncMock(return_value=state)):
            r = client.get("/api/sources/slack/disconnect",
                           cookies={COOKIE_NAME: _owner_cookie()},
                           follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/sources/slack"

    def test_source_without_db_secret_says_so(self):
        """У Trello ключ в infra/.env — гасить из дашборда нечего."""
        r = client.get("/api/sources/trello/disconnect",
                       cookies={COOKIE_NAME: _owner_cookie()})
        assert r.status_code == 400
        assert "не отключается" in r.text

    def test_unknown_source_does_not_500(self):
        r = client.get("/api/sources/nope/disconnect",
                       cookies={COOKIE_NAME: _owner_cookie()})
        assert r.status_code == 400


@pytest.mark.usefixtures("sqlite_db")
class TestDisconnect:

    async def _seed(self) -> None:
        from vera_shared.db.engine import get_session
        async with get_session() as s:
            s.add(SlackAuthRow(team_id="T1", team_name="Sintegrum Team",
                               username="dimondra", token_enc=encrypt("xoxp-live"),
                               is_active=True))
            s.add(EventRow(source="slack", source_event_id="C1:1.1",
                           occurred_at=__import__("datetime").datetime(2026, 8, 26),
                           content_text="Author: Я [self]\ntext",
                           triage_status="done"))

    async def _row(self) -> SlackAuthRow:
        from vera_shared.db.engine import get_session
        async with get_session() as s:
            return (await s.execute(select(SlackAuthRow))).scalar_one()

    @pytest.mark.asyncio
    async def test_deactivates_the_row(self):
        await self._seed()
        assert await source_state.disconnect("slack") == 1
        assert (await self._row()).is_active is False

    @pytest.mark.asyncio
    async def test_secret_is_kept_so_the_step_is_reversible(self):
        """Удалять токен нельзя: «отключить» должно быть обратимо, иначе это
        «удалить» под другим названием."""
        await self._seed()
        await source_state.disconnect("slack")
        row = await self._row()
        assert row.token_enc.startswith("enc1:")
        assert row.team_name == "Sintegrum Team"

    @pytest.mark.asyncio
    async def test_events_are_untouched(self):
        """Отключение останавливает приём, а не стирает память."""
        from vera_shared.db.engine import get_session
        await self._seed()
        await source_state.disconnect("slack")
        async with get_session() as s:
            left = (await s.execute(
                select(func.count()).select_from(EventRow)
                .where(EventRow.source == "slack")
            )).scalar_one()
        assert left == 1

    @pytest.mark.asyncio
    async def test_second_disconnect_is_a_noop(self):
        await self._seed()
        await source_state.disconnect("slack")
        assert await source_state.disconnect("slack") == 0

    @pytest.mark.asyncio
    async def test_source_without_secret_disconnects_nothing(self):
        assert await source_state.disconnect("trello") == 0
        assert await source_state.disconnect("nope") == 0


@pytest.mark.usefixtures("sqlite_db")
class TestStateReadsTheRealTable:
    """Состояние обязано приходить из таблицы доступа, а не из числа событий:
    Instagram с 353 событиями и мёртвой сессией подключённым не является."""

    @pytest.mark.asyncio
    async def test_no_row_is_not_connected(self):
        state = await source_state.state_of("slack")
        assert state.connected is False
        assert "токена нет" in state.label

    @pytest.mark.asyncio
    async def test_active_row_is_connected_and_names_the_workspace(self):
        from vera_shared.db.engine import get_session
        async with get_session() as s:
            s.add(SlackAuthRow(team_id="T1", team_name="Sintegrum Team",
                               username="dimondra", token_enc=encrypt("x"),
                               is_active=True))
        state = await source_state.state_of("slack")
        assert state.connected is True
        assert "Sintegrum Team" in state.label

    @pytest.mark.asyncio
    async def test_revoked_row_reports_the_reason(self):
        from vera_shared.db.engine import get_session
        async with get_session() as s:
            s.add(SlackAuthRow(team_id="T1", team_name="Sintegrum Team",
                               token_enc=encrypt("x"), is_active=False,
                               last_error="auth.test: token_revoked"))
        state = await source_state.state_of("slack")
        assert state.connected is False
        assert "token_revoked" in state.label

    @pytest.mark.asyncio
    async def test_internal_source_has_no_notion_of_connection(self):
        assert (await source_state.state_of("vera_memory")).connected is None
