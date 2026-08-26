"""Подключение Slack из дашборда: проверка токена до сохранения, шифрование,
и главное — токен не должен утечь ни в ответ, ни в лог.
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
from dashboard import slack_connect  # noqa: E402
from dashboard.app import app  # noqa: E402
from dashboard.auth import COOKIE_NAME  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402
from vera_shared.crypto import decrypt, is_encrypted  # noqa: E402
from vera_shared.db.models_sources import SlackAuthRow  # noqa: E402

client = TestClient(app)
TOKEN = "xoxp-secret-value-do-not-leak"
WHO = {"ok": True, "team_id": "T1", "team": "Sintegrum Team",
       "user_id": "U0ME", "user": "dima"}


def _owner_cookie() -> str:
    from dashboard.auth_routes import _set_session_cookie
    from starlette.responses import Response
    resp = Response()
    _set_session_cookie(resp)
    header = resp.headers.get("set-cookie", "")
    return header.split(";")[0].split("=", 1)[1] if "=" in header else ""


class TestAuthGate:
    def test_form_requires_owner(self):
        assert client.get("/api/slack/start").status_code == 401

    def test_post_requires_owner(self):
        r = client.post("/api/slack/start", data={"token": TOKEN})
        assert r.status_code == 401

    def test_form_shows_required_scopes(self):
        r = client.get("/api/slack/start", cookies={COOKIE_NAME: _owner_cookie()})
        assert r.status_code == 200
        for scope in ("channels:history", "im:history", "mpim:read", "files:read"):
            assert scope in r.text
        # Пользовательский токен, а не бот — это надо сказать прямо.
        assert "xoxp-" in r.text


class TestVerify:
    @pytest.mark.asyncio
    async def test_live_token_returns_workspace(self):
        class R:
            @staticmethod
            def json():
                return WHO

        async def fake_post(*a, **k):
            return R()

        with patch.object(slack_connect.httpx, "AsyncClient") as ac:
            ac.return_value.__aenter__ = AsyncMock(
                return_value=type("C", (), {"post": staticmethod(fake_post)})())
            ac.return_value.__aexit__ = AsyncMock(return_value=False)
            who, reason = await slack_connect.verify(TOKEN)
        assert reason is None
        assert who["team"] == "Sintegrum Team"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("code,expect", [
        ("invalid_auth", "не принят"),
        ("token_revoked", "отозван"),
        ("missing_scope", "не хватает прав"),
        ("weird_new_code", "weird_new_code"),
    ])
    async def test_refusal_reason_is_human_readable(self, code, expect):
        class R:
            @staticmethod
            def json():
                return {"ok": False, "error": code}

        async def fake_post(*a, **k):
            return R()

        with patch.object(slack_connect.httpx, "AsyncClient") as ac:
            ac.return_value.__aenter__ = AsyncMock(
                return_value=type("C", (), {"post": staticmethod(fake_post)})())
            ac.return_value.__aexit__ = AsyncMock(return_value=False)
            who, reason = await slack_connect.verify(TOKEN)
        assert who == {}
        assert expect in reason


@pytest.mark.usefixtures("sqlite_db")
class TestSave:

    async def _rows(self):
        from vera_shared.db.engine import get_session
        async with get_session() as s:
            return list((await s.execute(select(SlackAuthRow))).scalars().all())

    @pytest.mark.asyncio
    async def test_token_is_stored_encrypted_never_plaintext(self):
        await slack_connect.save_token(TOKEN, WHO)
        rows = await self._rows()
        assert len(rows) == 1
        assert is_encrypted(rows[0].token_enc)
        assert TOKEN not in rows[0].token_enc
        assert decrypt(rows[0].token_enc) == TOKEN

    @pytest.mark.asyncio
    async def test_workspace_metadata_is_stored(self):
        await slack_connect.save_token(TOKEN, WHO)
        row = (await self._rows())[0]
        assert (row.team_id, row.team_name) == ("T1", "Sintegrum Team")
        assert (row.user_id, row.username) == ("U0ME", "dima")
        assert row.is_active is True

    @pytest.mark.asyncio
    async def test_reconnect_updates_the_row_instead_of_adding_a_second(self):
        """Вторая строка на тот же воркспейс означала бы два токена и гонку
        за то, каким пойдёт обход."""
        await slack_connect.save_token(TOKEN, WHO)
        await slack_connect.save_token("xoxp-new-token", WHO)
        rows = await self._rows()
        assert len(rows) == 1
        assert decrypt(rows[0].token_enc) == "xoxp-new-token"

    @pytest.mark.asyncio
    async def test_reconnect_clears_the_previous_error(self):
        from vera_shared.db.engine import get_session
        await slack_connect.save_token(TOKEN, WHO)
        async with get_session() as s:
            row = (await s.execute(select(SlackAuthRow))).scalar_one()
            row.is_active, row.last_error = False, "token_revoked"
        await slack_connect.save_token(TOKEN, WHO)
        row = (await self._rows())[0]
        assert row.is_active is True
        assert row.last_error is None


class TestNoLeak:
    def test_rejected_token_is_not_echoed_back_into_the_form(self):
        with patch.object(slack_connect, "verify",
                          AsyncMock(return_value=({}, "токен не принят"))):
            r = client.post("/api/slack/start", data={"token": TOKEN},
                            cookies={COOKIE_NAME: _owner_cookie()})
        assert r.status_code == 400
        assert "токен не принят" in r.text
        assert TOKEN not in r.text

    def test_accepted_token_is_not_echoed_and_redirects_to_the_source(self):
        with patch.object(slack_connect, "verify",
                          AsyncMock(return_value=(WHO, None))), \
             patch.object(slack_connect, "save_token", AsyncMock()) as saved:
            r = client.post("/api/slack/start", data={"token": TOKEN},
                            cookies={COOKIE_NAME: _owner_cookie()},
                            follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/sources/slack"
        assert TOKEN not in r.text
        saved.assert_awaited_once()

    def test_token_never_reaches_the_log(self, caplog):
        with caplog.at_level("DEBUG"), \
             patch.object(slack_connect, "verify",
                          AsyncMock(return_value=({}, "токен не принят"))):
            client.post("/api/slack/start", data={"token": TOKEN},
                        cookies={COOKIE_NAME: _owner_cookie()})
        assert TOKEN not in caplog.text
